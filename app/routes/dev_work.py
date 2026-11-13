"""Feature-gated owner development queue and scoped worker/outbox API.

Owner intake originates from the verified Quo dispatch path, not customer SMS.
A separately authenticated worker may claim queued development jobs only when
explicit free-tier flags are active. Neither this API nor the worker permits
customer messaging, deployment, auto-merge, or arbitrary code execution.
"""
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Header
from sqlalchemy import or_

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dev_work_model import DevWorkEvent, DevWorkJob, DevWorkNotice
from app.routes.internal_messaging import require_internal_key

router = APIRouter()

PROJECTS = {"website", "operations", "finance", "integrations"}
MODELS = {"auto", "groq", "openai", "anthropic"}
_ACTIVE = ("queued", "needs_approval", "running", "review_needed", "blocked", "failed")
_REQUIRES_APPROVAL = re.compile(
    r"\b(?:merge|deploy|publish|launch|turn on|enable|send|email|text|message|"
    r"delete|erase|remove data|migrat(?:e|ion)|purchase|pay|charge|spend|"
    r"production|live customer|real customer|public form|domain)\b",
    re.IGNORECASE,
)


def capture_enabled() -> bool:
    return os.getenv("DEV_COORDINATOR_CAPTURE_ENABLED", "").strip().lower() == "true"


def worker_configured():
    """Configuration is not evidence a GitHub runner has started."""
    return (capture_enabled() and os.getenv("DEV_WORKER_ENABLED", "").lower() == "true"
            and os.getenv("GROQ_FREE_PLAN_CONFIRMED", "").lower() == "true"
            and bool(os.getenv("DEV_WORKER_KEY")))


def require_capture():
    if not capture_enabled():
        raise HTTPException(status_code=503, detail="Imperial development work capture is not activated.")
    return True


class QueueDevWork(BaseModel):
    messageId: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:\-]+$")
    projectKey: str = Field(min_length=2, max_length=32)
    instruction: str = Field(min_length=8, max_length=1600)
    modelPreference: str = Field(default="auto", max_length=16)


def serialise_job(job: DevWorkJob) -> dict:
    return {
        "id": job.id,
        "projectKey": job.project_key,
        "status": job.status,
        "modelPreference": job.model_preference,
        "latestSummary": job.latest_summary,
        "resultUrl": job.result_url,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
    }


def requires_owner_review(instruction: str) -> bool:
    return bool(_REQUIRES_APPROVAL.search(instruction))


@router.post("/submit")
def submit_development_work(
    payload: QueueDevWork,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_capture),
):
    project = payload.projectKey.strip().lower()
    model = payload.modelPreference.strip().lower()
    if project not in PROJECTS or model not in MODELS:
        raise HTTPException(status_code=422, detail="Unknown development project or model preference.")

    instruction = " ".join(payload.instruction.strip().split())
    if len(instruction) < 8:
        raise HTTPException(status_code=422, detail="A specific development instruction is required.")

    source, source_event = "quo_owner", payload.messageId
    existing = db.query(DevWorkJob).filter(
        DevWorkJob.source == source, DevWorkJob.external_id == source_event
    ).first()
    if existing:
        return {"recorded": True, "duplicate": True, **serialise_job(existing),
                "executionStarted": False if existing.status in ("queued", "needs_approval") else None,
                "workerConnected": False, "workerConfigured": worker_configured()}

    # The queue accepts an intent, not approval to change production or contact
    # customers. Risky requests are saved as needing explicit owner review.
    state = "needs_approval" if requires_owner_review(instruction) else "queued"
    job = DevWorkJob(
        source=source, external_id=source_event,
        project_key=project, instruction=instruction,
        model_preference=model, status=state,
        latest_summary=(
            "Requires owner review of sensitive action before execution."
            if state == "needs_approval" else
            "Saved; no autonomous coding worker is connected yet."
        ),
    )
    try:
        db.add(job)
        db.flush()
        db.add(DevWorkEvent(
            job_id=job.id, actor="owner_sms", action="captured",
            detail="Approval needed" if state == "needs_approval" else "Queued; not started",
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(DevWorkJob).filter(
            DevWorkJob.source == source, DevWorkJob.external_id == source_event
        ).first()
        if not existing:
            raise
        return {"recorded": True, "duplicate": True, **serialise_job(existing),
                "executionStarted": False, "workerConnected": False, "workerConfigured": worker_configured()}
    db.refresh(job)
    return {"recorded": True, "duplicate": False, **serialise_job(job),
            "executionStarted": False, "workerConnected": False, "workerConfigured": worker_configured()}


@router.get("/jobs")
def list_development_work(
    limit: int = 12,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_capture),
):
    if not 1 <= limit <= 25:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 25.")
    jobs = db.query(DevWorkJob).order_by(DevWorkJob.id.desc()).limit(limit).all()
    return {"jobs": [serialise_job(job) for job in jobs],
            "coverage": "Imperial development work queue only, not all GitHub or customer tasks.",
            "workerConnected": False,
            "workerConfigured": worker_configured(),
            "executionStartedByThisAPI": False}


def dashboard_snapshot(db: Session, role: str) -> dict:
    if role != "owner":
        return {"enabled": False, "jobs": [], "error": None}
    if not capture_enabled():
        return {"enabled": False, "jobs": [], "error": None}
    try:
        jobs = db.query(DevWorkJob).filter(DevWorkJob.status.in_(_ACTIVE)).order_by(
            DevWorkJob.id.desc()
        ).limit(8).all()
    except Exception:
        return {"enabled": True, "jobs": [], "error": "Development work records are unavailable. Verify migration."}
    return {"enabled": True, "jobs": [serialise_job(job) for job in jobs], "error": None,
            "workerConnected": False, "workerConfigured": worker_configured()}


# Scoped, separate worker identity. This key can claim/report development jobs,
# but cannot read customer/RO records or invoke any other internal API.
# The Groq free-tier confirmation is explicit: credential presence is NOT enough
# to establish the account plan or authorize billable execution.
def require_worker(x_dev_worker_key: str | None = Header(default=None)) -> bool:
    key = os.getenv("DEV_WORKER_KEY", "")
    if not key or not x_dev_worker_key or not secrets.compare_digest(key, x_dev_worker_key):
        raise HTTPException(status_code=401, detail="Invalid development-worker credential.")
    if os.getenv("DEV_WORKER_ENABLED", "").lower() != "true":
        raise HTTPException(status_code=503, detail="Development worker is not activated.")
    if os.getenv("GROQ_FREE_PLAN_CONFIRMED", "").lower() != "true":
        raise HTTPException(status_code=503, detail="Free-tier account is not confirmed.")
    if not capture_enabled():
        raise HTTPException(status_code=503, detail="Development queue is disabled.")
    return True


class WorkerClaim(BaseModel):
    workerId: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")


class WorkerReport(BaseModel):
    workerId: str = Field(pattern=r"^[a-zA-Z0-9_-]{8,64}$")
    status: str = Field(min_length=3, max_length=24)
    summary: str = Field(min_length=1, max_length=1500)
    resultUrl: str | None = Field(default=None, max_length=300)
    retryAfterSeconds: int | None = Field(default=None, ge=1, le=3600)


def _event(db: Session, job: DevWorkJob, actor: str, action: str, detail: str):
    event = DevWorkEvent(job_id=job.id, actor=actor, action=action, detail=detail[:1000])
    db.add(event)
    return event


def _prepare_notice(db: Session, job: DevWorkJob, event: DevWorkEvent):
    if os.getenv("DEV_WORK_NOTICES_ENABLED", "").lower() != "true":
        return
    if job.status not in ("review_needed", "blocked", "failed"):
        return
    db.flush()
    safe_summary = " ".join((job.latest_summary or "").split())[:250]
    message = f"Imperial development #{job.id} ({job.project_key}): {job.status.replace('_', ' ')}. {safe_summary}"
    if job.result_url:
        message += f" {job.result_url}"
    db.add(DevWorkNotice(event_id=event.id, job_id=job.id,
                         status="pending", text=message[:850]))



@router.post("/worker/claim")
def claim_worker_job(
    payload: WorkerClaim,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_worker),
):
    now = datetime.now(timezone.utc)
    # Never automatically reclaim a running lease: a delayed runner could still
    # push. Mark it blocked for manual investigation instead of double spending.
    expired = db.query(DevWorkJob).filter(
        DevWorkJob.status == "running", DevWorkJob.lease_until <= now
    ).with_for_update(skip_locked=True).limit(20).all()
    for orphan in expired:
        orphan.status = "blocked"
        orphan.latest_summary = "Worker lease expired; verify external run before retry."
        event = _event(db, orphan, "worker", "lease_expired", orphan.latest_summary)
        _prepare_notice(db, orphan, event)

    job = db.query(DevWorkJob).filter(
        DevWorkJob.status == "queued",
        DevWorkJob.project_key == "website",  # narrow free pilot only
        or_(DevWorkJob.retry_after.is_(None), DevWorkJob.retry_after <= now),
        DevWorkJob.attempt_count < 3,
        DevWorkJob.model_preference.in_(("auto", "groq")),
    ).order_by(DevWorkJob.id.asc()).with_for_update(skip_locked=True).first()
    if not job:
        db.commit()
        return {"claimed": False, "reason": "no_eligible_queued_work"}

    job.status = "running"
    job.assigned_worker = payload.workerId
    job.attempt_count = (job.attempt_count or 0) + 1
    job.lease_until = now + timedelta(minutes=25)
    job.retry_after = None
    job.updated_at = now
    job.latest_summary = "Claimed by free Groq pilot worker; no code result yet."
    _event(db, job, "worker", "claimed", f"attempt {job.attempt_count}")
    db.commit()
    return {
        "claimed": True, "id": job.id, "instruction": job.instruction,
        "projectKey": job.project_key, "modelPreference": job.model_preference,
        "attempt": job.attempt_count, "leaseUntil": job.lease_until.isoformat(),
    }


@router.post("/worker/jobs/{job_id}/report")
def report_worker_job(
    job_id: int,
    payload: WorkerReport,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_worker),
):
    if payload.status not in ("review_needed", "blocked", "failed", "rate_limited"):
        raise HTTPException(status_code=422, detail="Unrecognized worker result.")
    job = db.query(DevWorkJob).filter(DevWorkJob.id == job_id).with_for_update().first()
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown development job.")
    if job.status != "running" or not job.assigned_worker or not secrets.compare_digest(job.assigned_worker, payload.workerId):
        raise HTTPException(status_code=409, detail="Worker does not own an active job.")
    now = datetime.now(timezone.utc)
    lease_until = job.lease_until
    if lease_until is not None and lease_until.tzinfo is None:
        # SQLite returns naive datetimes for timezone-aware columns.
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until is None or lease_until <= now:
        raise HTTPException(status_code=409, detail="Worker lease is no longer active.")
    if payload.status == "review_needed" and not (
        payload.resultUrl and re.fullmatch(r"https://github\.com/imperialauto/shop/pull/[0-9]+", payload.resultUrl)
    ):
        raise HTTPException(status_code=422, detail="A verified repository pull request is required.")
    if payload.resultUrl and not re.fullmatch(r"https://github\.com/imperialauto/shop/pull/[0-9]+", payload.resultUrl):
        raise HTTPException(status_code=422, detail="Untrusted result URL.")
    if payload.status == "rate_limited" and payload.retryAfterSeconds is None:
        raise HTTPException(status_code=422, detail="A verified retry delay is required.")

    job.status = "queued" if payload.status == "rate_limited" and job.attempt_count < 3 else (
        "blocked" if payload.status == "rate_limited" else payload.status
    )
    job.latest_summary = payload.summary.strip()
    job.result_url = payload.resultUrl
    job.retry_after = (now + timedelta(seconds=payload.retryAfterSeconds)) if job.status == "queued" else None
    job.lease_until = None
    job.assigned_worker = None
    job.updated_at = now
    event = _event(db, job, "worker", payload.status, payload.summary)
    _prepare_notice(db, job, event)
    db.commit()
    return {"recorded": True, "id": job.id, "status": job.status, "workerConnected": True}


def require_notices():
    if os.getenv("DEV_WORK_NOTICES_ENABLED", "").lower() != "true" or not capture_enabled():
        raise HTTPException(status_code=503, detail="Owner development notices are disabled.")
    return True


class NoticeAck(BaseModel):
    claimNonce: str = Field(pattern=r"^[a-f0-9]{32}$")
    status: str = Field(pattern=r"^(sent|failed)$")


@router.post("/notices/claim")
def claim_owner_notice(
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_notices),
):
    # At-most-once delivery: claims NEVER expire into an automatic resend.
    # If Quo accepted a text and the ack was lost, retrying could duplicate it.
    notice = db.query(DevWorkNotice).filter(DevWorkNotice.status == "pending").order_by(
        DevWorkNotice.id.asc()
    ).with_for_update(skip_locked=True).first()
    if not notice:
        return {"notice": None}
    notice.status = "claimed"
    notice.claim_nonce = secrets.token_hex(16)
    notice.claimed_at = datetime.now(timezone.utc)
    db.commit()
    return {"notice": {"id": notice.id, "claimNonce": notice.claim_nonce, "text": notice.text}}


@router.post("/notices/{notice_id}/ack")
def ack_owner_notice(
    notice_id: int,
    payload: NoticeAck,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_notices),
):
    notice = db.query(DevWorkNotice).filter(DevWorkNotice.id == notice_id).with_for_update().first()
    if not notice or notice.status != "claimed" or not notice.claim_nonce or not secrets.compare_digest(
        notice.claim_nonce, payload.claimNonce
    ):
        raise HTTPException(status_code=409, detail="Notice claim is not active.")
    notice.status = payload.status
    if payload.status == "sent":
        notice.delivered_at = datetime.now(timezone.utc)
    notice.claim_nonce = None
    db.commit()
    return {"recorded": True, "status": notice.status}
