"""Owner-only development work queue (capture/list), not a coding executor.

Inbound requests originate from the verified owner's Quo dispatch path via the
messaging service. Requests do not start Claude, OpenAI, deployments, timers,
outbound texts or customer activity. No worker is connected in this release.
"""
import os
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.dev_work_model import DevWorkEvent, DevWorkJob
from app.routes.internal_messaging import require_internal_key

router = APIRouter()

PROJECTS = {"website", "operations", "finance", "integrations"}
MODELS = {"auto", "openai", "anthropic"}
_ACTIVE = ("queued", "needs_approval", "running", "blocked")
_REQUIRES_APPROVAL = re.compile(
    r"\b(?:merge|deploy|publish|launch|turn on|enable|send|email|text|message|"
    r"delete|erase|remove data|migrat(?:e|ion)|purchase|pay|charge|spend|"
    r"production|live customer|real customer|public form|domain)\b",
    re.IGNORECASE,
)


def capture_enabled() -> bool:
    return os.getenv("DEV_COORDINATOR_CAPTURE_ENABLED", "").strip().lower() == "true"


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
                "workerConnected": False}

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
                "executionStarted": False, "workerConnected": False}
    db.refresh(job)
    return {"recorded": True, "duplicate": False, **serialise_job(job),
            "executionStarted": False, "workerConnected": False}


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
            "workerConnected": False}
