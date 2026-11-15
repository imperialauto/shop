"""Owner task API for Imperial SMS service-to-service calls only.

Feature-flagged and blocked by APP_READ_ONLY. Never sends a customer message.
Caller is the already owner-verified Quo dispatch path on the trusted messaging
service; X-Internal-Key alone must remain secret and least-privileged.
"""
import os
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.imperial_task_model import PROBLEM_CATEGORY, ImperialTask, ImperialTaskAudit
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
    dueAt: AwareDatetime | None = None


def task_record(task):
    due = task.due_at
    if due is not None and due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return {
        "id": task.id, "title": task.title, "status": task.status,
        "projectKey": task.project_key, "assignedUserId": task.assigned_user_id,
        "priority": task.priority, "dueAt": due.isoformat() if due else None,
    }


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
        return {"recorded": True, "duplicate": True, **task_record(existing)}

    task = ImperialTask(source=source, external_id=event, title=title,
                        project_key=payload.projectKey, due_at=payload.dueAt.astimezone(timezone.utc) if payload.dueAt else None, status="open")
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
            return {"recorded": True, "duplicate": True, **task_record(existing)}
        raise
    return {"recorded": True, "duplicate": False, **task_record(task)}


def owner_dashboard_tasks(db: Session, role: str) -> dict:
    """Owner-only snapshot for the existing Needs Attention section.

    Missing migration or query errors are NOT reported as an empty task list.
    Disabled means no query against a possibly unmigrated production database.
    """
    if role != "owner" or not enabled():
        return {"enabled": False, "tasks": [], "error": None}
    try:
        rows = db.query(ImperialTask).filter(
            ImperialTask.status.in_(("open", "in_progress", "blocked")),
            ImperialTask.category != PROBLEM_CATEGORY,
        ).order_by(ImperialTask.id.desc()).limit(12).all()
    except Exception:
        # Do not expose database exception detail through the dashboard.
        return {"enabled": True, "tasks": [], "error": "Saved task records could not be loaded."}
    return {"enabled": True, "tasks": [
        {"id": row.id, "title": row.title, "status": row.status,
         "project_key": row.project_key, "priority": row.priority}
        for row in rows
    ], "error": None}


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
        ImperialTask.category != PROBLEM_CATEGORY,
    ).order_by(ImperialTask.id.desc()).limit(limit).all()
    return {"tasks": [task_record(task) for task in rows],
            "coverage": "Imperial internal tasks only; Quo and GitHub tasks may be separate."}


class OwnerTaskChange(BaseModel):
    messageId: str = Field(min_length=1, max_length=128)
    operation: Literal["complete", "reopen", "reschedule", "wait"]
    dueAt: AwareDatetime | None = None


def change_task(db, task_id, change, actor):
    task = db.query(ImperialTask).filter_by(id=task_id).with_for_update().first()
    if not task:
        raise HTTPException(404, "Imperial task not found.")
    prior = db.query(ImperialTaskAudit).filter_by(
        task_id=task_id, actor=actor, event_id=change.messageId,
    ).first()
    if prior:
        return task_record(task)
    if change.operation == "reschedule":
        if change.dueAt is None:
            raise HTTPException(422, "A date and time are required.")
        task.due_at = change.dueAt.astimezone(timezone.utc)
    elif change.operation == "wait":
        task.status = "blocked"  # shown as "Waiting on them"
    else:
        task.status = "done_verified" if change.operation == "complete" else "open"
    task.updated_at = datetime.now(timezone.utc)
    db.add(ImperialTaskAudit(
        task_id=task_id, actor=actor, action=change.operation, event_id=change.messageId,
    ))
    db.commit()
    return task_record(task)


@router.post("/{task_id}/change")
def change_owner_task(
    task_id: int, change: OwnerTaskChange, db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    return change_task(db, task_id, change, "owner_sms")


def create_dashboard_task(db, owner_id, title, due):
    title = " ".join(title.split())
    if not 4 <= len(title) <= 200:
        raise HTTPException(422, "Enter a task title between 4 and 200 characters.")
    event_id = uuid4().hex
    task = ImperialTask(title=title, source="dashboard", external_id=event_id,
                        due_at=due.astimezone(timezone.utc) if due else None)
    db.add(task)
    db.flush()
    db.add(ImperialTaskAudit(
        task_id=task.id, actor=f"owner:{owner_id}", action="created", event_id=event_id,
    ))
    db.commit()

@router.get("/problems")
def list_problem_reports(
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    """For the developers and agents who work the fix list."""
    return {"problems": [{**task_record(task), "details": task.details} for task in problem_reports(db)]}


@router.get("/{task_id}")
def read_owner_task(
    task_id: int, db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    task = db.get(ImperialTask, task_id)
    if not task:
        raise HTTPException(404, "Imperial task not found.")
    return task_record(task)


class QuoTaskBatch(BaseModel):
    tasks: list[dict] = Field(default_factory=list, max_length=1000)


class QuoTaskAck(BaseModel):
    completed: list[str] = Field(default_factory=list, max_length=200)


@router.post("/quo-sync")
def quo_sync(
    payload: QuoTaskBatch, db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    """Messaging service posts Quo's task list; gets back tasks to mark done in Quo."""
    from app import quo_task_sync
    return quo_task_sync.sync(db, payload.tasks)


@router.post("/quo-sync/ack")
def quo_sync_ack(
    payload: QuoTaskAck, db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
    _enabled: bool = Depends(require_tasks_enabled),
):
    from app import quo_task_sync
    return {"acknowledged": quo_task_sync.acknowledge(db, payload.completed)}


# ── App problem reports ("Report a problem" button) ─────────────────────────
# Stored in the existing task table under their own category, so no new
# migration is needed and they never mix into the owner's to-do lists.
PROBLEM_SOURCE = "problem_report"


def create_problem_report(db, user, page: str, comment: str) -> ImperialTask:
    comment = (comment or "").strip()
    if not 3 <= len(comment) <= 4000:
        raise HTTPException(422, "Describe the problem in 3 to 4000 characters.")
    page = (page or "")[:300]
    title = " ".join(comment.split())[:200]
    event_id = uuid4().hex
    task = ImperialTask(
        title=title, source=PROBLEM_SOURCE, external_id=event_id, category=PROBLEM_CATEGORY,
        project_key="operations",
        details=f"Page: {page}\nReported by: {getattr(user, 'username', None) or getattr(user, 'id', '?')}\n\n{comment}",
    )
    db.add(task)
    db.flush()
    db.add(ImperialTaskAudit(task_id=task.id, actor=f"user:{getattr(user, 'id', '?')}", action="reported", event_id=event_id))
    db.commit()
    return task


def problem_reports(db, include_fixed: bool = False) -> list[ImperialTask]:
    statuses = ("open", "in_progress", "blocked") + (("done_verified",) if include_fixed else ())
    return db.query(ImperialTask).filter(
        ImperialTask.category == PROBLEM_CATEGORY, ImperialTask.status.in_(statuses),
    ).order_by(ImperialTask.id.desc()).limit(200).all()
