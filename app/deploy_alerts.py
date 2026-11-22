"""Railway tells the dashboard when a deploy fails or the app crashes.

Railway posts to /webhooks/railway/<token> whenever a service's deployment
changes state. A failure or crash:
  - shows as a problem on the Home health line ("App deploys"), and
  - opens one owner task on My Day per service ("Fix the <service> app"),
so the owner sees it in the app. Nothing is texted or emailed.
The next good deploy of that service clears the Home problem and closes the task.

Preview (PR) environments are ignored. Uses the existing dispatch_sync and
imperial_tasks tables, so no database change. Never contacts a customer.
The caller commits.
"""
import hmac
import os
from datetime import datetime, timezone

from app.dispatch_model import DispatchSync
from app.imperial_task_model import PERSONAL_CATEGORY, ImperialTask, ImperialTaskAudit
from app.models import User

TOKEN_ENV = "RAILWAY_WEBHOOK_TOKEN"
MIN_TOKEN_LENGTH = 24
SOURCE_PREFIX = "deploy:"  # dispatch_sync.source rows, one per service
TASK_SOURCE = "railway"
OPEN = ("open", "in_progress", "blocked")

BAD = {"failed", "crashed", "oom_killed", "oomkilled", "removed_on_failure"}
GOOD = {"deployed", "success", "succeeded", "redeployed", "restarted"}
WORDS = {"failed": "failed to deploy", "crashed": "crashed", "oom_killed": "ran out of memory",
         "oomkilled": "ran out of memory", "removed_on_failure": "failed to deploy"}


def token_ok(token: str) -> bool:
    expected = os.getenv(TOKEN_ENV, "")
    if len(expected) < MIN_TOKEN_LENGTH:
        return False  # off until a strong token is set in Railway
    return hmac.compare_digest(token.encode(), expected.encode())


def _status(payload: dict) -> str:
    """'Deployment.failed' -> 'failed'. Falls back to details.status / status (older format)."""
    kind = str(payload.get("type") or "")
    if "." in kind:
        return kind.rsplit(".", 1)[1].strip().lower()
    details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
    return str(details.get("status") or payload.get("status") or "").strip().lower()


def _resource(payload: dict, name: str) -> dict:
    resource = payload.get("resource") if isinstance(payload.get("resource"), dict) else {}
    value = resource.get(name) or payload.get(name)
    return value if isinstance(value, dict) else {}


def parse(payload: dict) -> dict | None:
    """Returns {service, state: 'bad'|'good', status, environment} or None to ignore."""
    if not isinstance(payload, dict):
        return None
    environment = _resource(payload, "environment")
    if environment.get("isEphemeral"):
        return None  # preview builds are not the live app
    status = _status(payload)
    state = "bad" if status in BAD else "good" if status in GOOD else None
    service = str(_resource(payload, "service").get("name") or "").strip()
    if not state or not service:
        return None
    return {"service": service[:60], "state": state, "status": status,
            "environment": str(environment.get("name") or "")[:40]}


def _owner_id(db):
    owner = db.query(User).filter(User.role == "owner").order_by(User.id).first()
    return owner.id if owner else None


def _tasks_enabled() -> bool:
    from app.routes.imperial_tasks import enabled
    return enabled()


def _task(db, service):
    return db.query(ImperialTask).filter_by(source=TASK_SOURCE, external_id=service[:64]).first()


def record(db, payload: dict, now: datetime | None = None) -> dict | None:
    event = parse(payload)
    if event is None:
        return None
    now = now or datetime.now(timezone.utc)
    service = event["service"]
    key = (SOURCE_PREFIX + service)[:32]
    row = db.get(DispatchSync, key)
    if row is None:
        row = DispatchSync(source=key, consecutive_failures=0)
        db.add(row)
    if event["state"] == "bad":
        row.last_failure_at = now
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.last_detail = f"The {service} app {WORDS.get(event['status'], event['status'])}."[:255]
        _open_task(db, service, row.last_detail, now)
    else:
        row.last_success_at = now
        row.consecutive_failures = 0
        row.last_detail = f"The {service} app is running."[:255]
        _close_task(db, service, now)
    db.flush()
    return event


def _open_task(db, service, detail, now):
    if not _tasks_enabled():
        return None
    task = _task(db, service)
    title = f"Fix the {service} app: Railway says it went down"[:200]
    if task is None:
        task = ImperialTask(source=TASK_SOURCE, external_id=service[:64], category=PERSONAL_CATEGORY,
                            assigned_user_id=_owner_id(db), status="open", priority="high", title=title)
        db.add(task)
        action = "created"
    elif task.status in OPEN:
        action = "updated"
    else:
        task.status, action = "open", "reopened"
    task.title = title
    task.details = detail + " It closes on its own after the next good deploy. Check the Railway logs, or ask Claude."
    task.due_at = now
    task.updated_at = now
    db.flush()
    db.add(ImperialTaskAudit(task_id=task.id, actor="railway", action=action,
                             event_id=f"railway:{service}:bad:{now.isoformat()}"[:128]))
    return task


def _close_task(db, service, now):
    if not _tasks_enabled():
        return None
    task = _task(db, service)
    if task is None or task.status not in OPEN:
        return task
    task.status, task.updated_at = "done_verified", now
    db.add(ImperialTaskAudit(task_id=task.id, actor="railway", action="recovered",
                             event_id=f"railway:{service}:good:{now.isoformat()}"[:128]))
    return task


def down_services(db) -> list:
    """Services whose latest Railway news is a failure."""
    rows = db.query(DispatchSync).filter(DispatchSync.source.like(SOURCE_PREFIX + "%")).all()
    return [r for r in rows if (r.consecutive_failures or 0) > 0]


def any_reported(db) -> bool:
    return db.query(DispatchSync).filter(DispatchSync.source.like(SOURCE_PREFIX + "%")).first() is not None
