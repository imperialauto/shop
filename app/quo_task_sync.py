"""Quo tasks show up in Master Tasks, and finishing one in Imperial finishes it in Quo.

The messaging service reads Quo's task list on a timer and posts it here. Each Quo task is
saved once as an Imperial task (source "quo", Quo's task id as the external id), so the same
task is never added twice. Tasks for the same customer conversation are shown as one card.

Two-way rules:
- Done in Quo -> done in Imperial.
- Done in Imperial -> listed back to the messaging service, which marks it done in Quo and
  confirms with an acknowledgement (audited as "quo_completed").
- Reopened in Imperial after Quo finished it -> stays open in Imperial (Quo has no reopen).
No customer is ever texted from here. Uses existing tables only (no migration).
"""
import json
import re
from datetime import datetime, timezone

from app.imperial_task_model import ImperialTask, ImperialTaskAudit

SOURCE = "quo"
OPEN = ("open", "in_progress", "blocked")
ACTOR = "quo_sync"

# "[REPLY DRAFT — UPDATE] 2003 Suburban — ..." -> ("Reply draft", "2003 Suburban — ...")
TAG = re.compile(r"^\s*\[([^\]]{2,60})\]\s*")
PHONE = re.compile(r"\+?1?\s*\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})")

KINDS = {
    "reply": "Reply", "call back": "Call back", "new lead": "New lead", "awaiting": "Waiting",
    "schedule": "Schedule", "admin": "Admin", "verify": "Check", "pay yard": "Parts",
    "price": "Price check", "call review": "Call review", "in shop": "In shop", "open": "Check",
}


def _parse_time(value):
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def label(title: str) -> dict:
    """Split Quo's bracketed tag from the readable title."""
    match = TAG.match(title or "")
    tag = match.group(1).strip() if match else ""
    text = TAG.sub("", title or "", count=1).strip() if match else (title or "").strip()
    lowered = tag.lower()
    kind = next((name for key, name in KINDS.items() if key in lowered), tag.title() if tag else "Task")
    return {"kind": kind, "update": "update" in lowered, "urgent": "urgent" in lowered or lowered.startswith("p1"),
            "text": text or title}


def phone_in(title: str) -> str | None:
    match = PHONE.search(title or "")
    return f"+1{match.group(1)}{match.group(2)}{match.group(3)}" if match else None


def details(task: ImperialTask) -> dict:
    try:
        return json.loads(task.details or "{}")
    except ValueError:
        return {}


def _audit(db, task, action, event):
    db.add(ImperialTaskAudit(task_id=task.id, actor=ACTOR, action=action, event_id=event[:128]))


def _last(db, task_id, actions):
    return (db.query(ImperialTaskAudit)
            .filter(ImperialTaskAudit.task_id == task_id, ImperialTaskAudit.action.in_(actions))
            .order_by(ImperialTaskAudit.id.desc()).first())


def sync(db, tasks: list[dict]) -> dict:
    """Save Quo's tasks; return the Quo task ids Imperial finished that Quo still has open."""
    added = updated = closed = 0
    seen = set()
    for raw in tasks:
        task_id = str(raw.get("taskId") or raw.get("id") or "").strip()
        title = " ".join(str(raw.get("title") or "").split())[:200]
        if not task_id or task_id in seen or len(title) < 2:
            continue
        seen.add(task_id)
        quo_done = bool(raw.get("completedAt")) or str(raw.get("status") or "").lower() in (
            "completed", "done", "complete", "closed", "resolved")
        info = {"conversationId": raw.get("conversationId"), "phoneNumberId": raw.get("phoneNumberId"),
                "activityId": raw.get("activityId"), "assignedTo": raw.get("assignedTo"),
                "quoStatus": "completed" if quo_done else "open",
                "quoCreatedAt": raw.get("createdAt"), "quoCompletedAt": raw.get("completedAt"),
                "phone": phone_in(title)}
        due = _parse_time(raw.get("dueDate"))
        task = db.query(ImperialTask).filter(ImperialTask.source == SOURCE, ImperialTask.external_id == task_id).first()
        now = datetime.now(timezone.utc)
        if task is None:
            task = ImperialTask(source=SOURCE, external_id=task_id, title=title, category="customer",
                                priority="urgent" if label(title)["urgent"] else "normal",
                                status="done_verified" if quo_done else ("blocked" if label(title)["kind"] == "Waiting" else "open"),
                                due_at=due,
                                details=json.dumps(info), created_at=_parse_time(raw.get("createdAt")) or now,
                                updated_at=now)
            db.add(task)
            db.flush()
            _audit(db, task, "created", f"quo:{task_id}")
            added += 1
            continue
        before = (task.title, task.details, task.due_at)
        previous = details(task)
        task.title, task.details = title, json.dumps({**previous, **info})
        if due is not None:
            task.due_at = due
        if quo_done and task.status in OPEN:
            reopened = _last(db, task.id, ("reopen",))
            # Owner reopened it here after Quo finished it: keep it open in Imperial.
            if not (reopened and previous.get("quoStatus") == "completed"):
                task.status = "done_verified"
                _audit(db, task, "complete", f"quo-done:{task_id}")
                closed += 1
        if (task.title, task.details, task.due_at) != before:
            task.updated_at = now
            updated += 1
    db.commit()
    return {"added": added, "updated": updated, "closed": closed, "complete_in_quo": pending(db)}


def pending(db) -> list[str]:
    """Quo tasks finished in Imperial but still open in Quo."""
    rows = db.query(ImperialTask).filter(ImperialTask.source == SOURCE, ImperialTask.status == "done_verified").all()
    out = []
    for task in rows:
        if details(task).get("quoStatus") == "completed":
            continue
        last_done = _last(db, task.id, ("complete",))
        acked = _last(db, task.id, ("quo_completed",))
        if last_done and (acked is None or acked.id < last_done.id):
            out.append(task.external_id)
    return out


def acknowledge(db, task_ids: list[str]) -> int:
    count = 0
    for task_id in task_ids[:200]:
        task = db.query(ImperialTask).filter(ImperialTask.source == SOURCE, ImperialTask.external_id == str(task_id)).first()
        if task is None:
            continue
        info = details(task)
        info["quoStatus"] = "completed"
        task.details = json.dumps(info)
        _audit(db, task, "quo_completed", f"ack:{task_id}:{datetime.now(timezone.utc).timestamp()}")
        count += 1
    db.commit()
    return count
