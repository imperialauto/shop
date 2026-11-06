"""Owner task API for Imperial SMS service-to-service calls only.

Feature-flagged and blocked by APP_READ_ONLY. Never sends a customer message.
Caller is the already owner-verified Quo dispatch path on the trusted messaging
service; X-Internal-Key alone must remain secret and least-privileged.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.imperial_task_model import ImperialTask, ImperialTaskAudit
from app.routes.internal_messaging import require_internal_key

router = APIRouter()


def enabled():
    return os.getenv("IMPERIAL_TASKS_ENABLED", "").strip().lower() == "true"


def require_tasks_enabled():
    if not enabled():
        raise HTTPException(status_code=503, detail="Imperial task storage has not been activated.")
    return True


class CaptureOwnerTask(BaseModel):
    messageId: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=4, max_length=200)
    projectKey: str | None = Field(default=None, pattern="^[a-z0-9-]{1,64}$")


@router.post("/capture")
def capture_owner_task(
    payload: CaptureOwnerTask,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    title = " ".join(payload.title.strip().split())
    if len(title) < 4:
        raise HTTPException(status_code=422, detail="Task requires a meaningful title.")
    # Stable Quo message ID prevents duplicated tasks on webhook retries.
    source, event = "quo_owner", payload.messageId
    existing = db.query(ImperialTask).filter(
        ImperialTask.source == source, ImperialTask.external_id == event,
    ).first()
    if existing:
        return {"recorded": True, "duplicate": True, "id": existing.id, "title": existing.title,
                "status": existing.status}

    task = ImperialTask(source=source, external_id=event, title=title,
                        project_key=payload.projectKey, status="open")
    try:
        db.add(task)
        db.flush()
        db.add(ImperialTaskAudit(
            task_id=task.id, actor="owner_sms", action="created", event_id=event,
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(ImperialTask).filter(
            ImperialTask.source == source, ImperialTask.external_id == event,
        ).first()
        if existing:
            return {"recorded": True, "duplicate": True, "id": existing.id,
                    "title": existing.title, "status": existing.status}
        raise
    return {"recorded": True, "duplicate": False, "id": task.id, "title": task.title,
            "status": task.status}


@router.get("/open")
def open_owner_tasks(
    limit: int = 15,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    if limit < 1 or limit > 25:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 25.")
    rows = db.query(ImperialTask).filter(
        ImperialTask.status.in_(("open", "in_progress", "blocked")),
    ).order_by(ImperialTask.id.desc()).limit(limit).all()
    return {"tasks": [{"id": t.id, "title": t.title, "status": t.status,
                       "projectKey": t.project_key, "assignedUserId": t.assigned_user_id,
                       "dueAt": t.due_at.isoformat() if t.due_at else None} for t in rows],
            "coverage": "Imperial internal tasks only; Quo and GitHub tasks may be separate."}
