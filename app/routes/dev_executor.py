"""Opt-in, owner-authorized development executions. No production executor."""
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, Index, text
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.database import Base, get_db
from app.dev_work_model import DevWorkJob, DevWorkEvent
from app.routes.internal_messaging import require_internal_key

router = APIRouter()


class DevExecution(Base):
    __tablename__ = 'dev_executions'
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey('dev_work_jobs.id'), unique=True, nullable=False)
    worker_id = Column(String(64), nullable=False)
    mode = Column(String(16), nullable=False)
    base_sha = Column(String(40), nullable=False)
    state = Column(String(24), nullable=False, default='approved')
    claim_id = Column(String(64), unique=True)
    token_hash = Column(String(64))
    lease_until = Column(DateTime(timezone=True))
    deadline = Column(DateTime(timezone=True))
    evidence = Column(Text)
    __table_args__ = (Index('uq_dev_running_host', 'worker_id', unique=True,
                           postgresql_where=text("state = 'running'"),
                           sqlite_where=text("state = 'running'")),)


def enabled():
    if os.getenv('DEV_EXECUTOR_ENABLED', '').lower() != 'true':
        raise HTTPException(503, 'Development execution is disabled.')


def worker_auth(x_dev_worker_key: str = Header(default='')):
    enabled()
    key = os.getenv('DEV_EXECUTOR_KEY', '')
    if len(key) < 32 or not secrets.compare_digest(key, x_dev_worker_key):
        raise HTTPException(401, 'Invalid development worker credential.')


def now():
    return datetime.now(timezone.utc)


def utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def event(db, run, action, detail):
    db.add(DevWorkEvent(job_id=run.job_id, actor='dev_executor', action=action, detail=detail))


class Approval(BaseModel):
    workerId: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    baseSha: str = Field(pattern=r'^[a-f0-9]{40}$')
    mode: Literal['synthetic', 'local_patch'] = 'synthetic'


@router.post('/jobs/{job_id}/authorize')
def authorize(job_id: int, payload: Approval, db: Session = Depends(get_db),
              auth=Depends(require_internal_key), flag=Depends(enabled)):
    job = db.query(DevWorkJob).filter_by(id=job_id).with_for_update().first()
    if not job or job.status not in ('queued', 'needs_approval'):
        raise HTTPException(409, 'Job is not awaiting development authorization.')
    if db.query(DevExecution).filter_by(job_id=job_id).first():
        raise HTTPException(409, 'Execution already authorized; do not duplicate.')
    if job.project_key != 'website':
        raise HTTPException(422, 'Initial executor supports website work only.')
    run = DevExecution(job_id=job_id, worker_id=payload.workerId,
                       mode=payload.mode, base_sha=payload.baseSha, state='approved')
    db.add(run)
    job.status = 'queued'
    job.latest_summary = 'Development authorized; awaiting assigned worker.'
    event(db, run, 'authorized', json.dumps(payload.model_dump()))
    db.commit()
    return {'authorized': True, 'jobId': job_id}


class Claim(BaseModel):
    workerId: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,64}$')
    claimId: str = Field(pattern=r'^[a-f0-9]{32}$')
    token: str = Field(pattern=r'^[a-f0-9]{64}$')


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def snapshot(run, job):
    return {'claimed': True, 'id': run.id, 'jobId': run.job_id, 'mode': run.mode,
            'baseSha': run.base_sha, 'state': run.state, 'instruction': job.instruction,
            'evidence': json.loads(run.evidence) if run.evidence else None}


@router.post('/claim')
def claim(payload: Claim, db: Session = Depends(get_db), auth=Depends(worker_auth)):
    run = db.query(DevExecution).filter_by(claim_id=payload.claimId).with_for_update().first()
    if run:
        if run.worker_id != payload.workerId or not secrets.compare_digest(run.token_hash, digest(payload.token)):
            raise HTTPException(409, 'Claim belongs to another worker.')
        return snapshot(run, db.get(DevWorkJob, run.job_id))
    # Pin jobs to one host. Expired executions NEVER move to another host: the
    # original may still be running while disconnected.
    active = db.query(DevExecution).filter_by(worker_id=payload.workerId, state='running').first()
    if active:
        raise HTTPException(409, 'Recover the existing execution before claiming more work.')
    run = db.query(DevExecution).filter_by(worker_id=payload.workerId, state='approved').order_by(
        DevExecution.id).with_for_update(skip_locked=True).first()
    if not run:
        return {'claimed': False}
    run.claim_id, run.token_hash = payload.claimId, digest(payload.token)
    run.state = 'running'
    run.lease_until, run.deadline = now() + timedelta(minutes=2), now() + timedelta(hours=24)
    job = db.get(DevWorkJob, run.job_id)
    job.status, job.assigned_worker = 'running', run.worker_id
    job.latest_summary = 'Development worker claimed the job; result pending.'
    event(db, run, 'claimed', 'One host-bound execution claimed.')
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, 'Concurrent claim; retry the same durable claim identity.')
    return snapshot(run, job)


def owned(db, run_id, payload):
    run = db.query(DevExecution).filter_by(id=run_id).with_for_update().first()
    if not run or run.worker_id != payload.workerId or run.claim_id != payload.claimId or not run.token_hash or not secrets.compare_digest(run.token_hash, digest(payload.token)):
        raise HTTPException(409, 'Execution ownership mismatch.')
    return run


@router.post('/runs/{run_id}/heartbeat')
def heartbeat(run_id: int, payload: Claim, db: Session = Depends(get_db), auth=Depends(worker_auth)):
    run = owned(db, run_id, payload)
    if run.state != 'running' or now() >= utc(run.deadline):
        raise HTTPException(409, 'Execution is no longer renewable.')
    # Only the same durable claim can renew after interruption. Worker must
    # hold its OS lock and reconcile its local journal before doing more work.
    if now() >= utc(run.lease_until):
        event(db, run, 'recovered', 'Same host and durable claim renewed after interruption.')
    run.lease_until = min(run.deadline if run.deadline.tzinfo else utc(run.deadline), now() + timedelta(minutes=2))
    db.commit()
    return {'renewed': True}


class Result(Claim):
    outcome: Literal['review_needed', 'blocked', 'failed']
    candidateSha: str | None = Field(default=None, pattern=r'^[a-f0-9]{40}$')
    artifactHash: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    checks: Literal['synthetic_contract_passed', 'bounded_patch_validated', 'not_run']


@router.post('/runs/{run_id}/result')
def result(run_id: int, payload: Result, db: Session = Depends(get_db), auth=Depends(worker_auth)):
    run = owned(db, run_id, payload)
    evidence = json.dumps(payload.model_dump(exclude={'token', 'claimId', 'workerId'}), sort_keys=True)
    if run.evidence:
        if run.evidence != evidence:
            raise HTTPException(409, 'A different final outcome is already recorded.')
        return {'recorded': True, 'duplicate': True}
    if run.state != 'running' or now() >= utc(run.lease_until):
        raise HTTPException(409, 'Lease expired; recover before reporting.')
    expected = 'synthetic_contract_passed' if run.mode == 'synthetic' else 'bounded_patch_validated'
    if payload.outcome == 'review_needed' and (not payload.candidateSha or not payload.artifactHash or payload.checks != expected):
        raise HTTPException(422, 'Candidate evidence is incomplete.')
    run.state, run.evidence = payload.outcome, evidence
    job = db.get(DevWorkJob, run.job_id)
    job.status = payload.outcome
    job.latest_summary = f'{run.mode}: {payload.outcome}; {payload.checks}. Candidate {payload.candidateSha or "none"}. Not merged or deployed.'
    job.updated_at = now()
    event(db, run, 'final_outcome', evidence)
    db.commit()
    return {'recorded': True, 'duplicate': False}


@router.get('/jobs/{job_id}')
def inspect(job_id: int, db: Session = Depends(get_db), auth=Depends(require_internal_key), flag=Depends(enabled)):
    run = db.query(DevExecution).filter_by(job_id=job_id).first()
    if not run:
        raise HTTPException(404, 'No execution authorized.')
    data = snapshot(run, db.get(DevWorkJob, job_id))
    data['workerOnline'] = bool(run.state == 'running' and run.lease_until and utc(run.lease_until) > now())
    data['events'] = [{'action': e.action, 'detail': e.detail} for e in db.query(DevWorkEvent).filter_by(job_id=job_id).order_by(DevWorkEvent.id)]
    return data
