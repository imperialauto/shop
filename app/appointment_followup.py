"""When an appointment is confirmed, the owner gets a reminder task for it.

The task sits on My Day ("Next" once it is within three hours) and on Master Tasks,
due at the appointment's start. One task per appointment: confirming twice, or a
retry, finds the same task. Cancelling, marking missed or completing the visit
closes it, so nothing is left hanging.

Uses the existing imperial_tasks table; does nothing while IMPERIAL_TASKS_ENABLED
is off. Never contacts a customer. The caller commits.
"""
from datetime import datetime, timezone

from app.appointments import PHOENIX
from app.imperial_task_model import PERSONAL_CATEGORY, ImperialTask, ImperialTaskAudit
from app.models import RepairOrder, User

SOURCE = "appointment"
OPEN = ("open", "in_progress", "blocked")


def _enabled() -> bool:
    from app.routes.imperial_tasks import enabled
    return enabled()


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def first_name(customer) -> str:
    """First name only. Records still named "Unknown (last 4)" show as Customer."""
    name = (getattr(customer, "first_name", "") or "").strip()
    return "Customer" if not name or name.lower().startswith("unknown") else name


def when_label(starts_at) -> str:
    local = _aware(starts_at).astimezone(PHOENIX)
    return local.strftime("%a %b %-d, %-I:%M %p")


def title_for(appointment, order) -> str:
    who = first_name(order.customer) if order else "Customer"
    vehicle = order.vehicle.label if order and order.vehicle else "Vehicle details not given yet"
    where = " (mobile)" if appointment.location == "mobile" else ""
    return f"Appointment {when_label(appointment.starts_at)}{where}: {who}, {vehicle}, job #{appointment.repair_order_id}"[:200]


def _owner_id(db):
    owner = db.query(User).filter(User.role == "owner").order_by(User.id).first()
    return owner.id if owner else None


def _task(db, appointment_id):
    return db.query(ImperialTask).filter_by(source=SOURCE, external_id=str(appointment_id)).first()


def add_reminder(db, appointment, actor: str):
    """Create (or reopen and re-time) the owner's task for a confirmed appointment."""
    if not _enabled():
        return None
    order = db.get(RepairOrder, appointment.repair_order_id)
    title = title_for(appointment, order)  # before adding: loading names would flush a half-made row
    now = datetime.now(timezone.utc)
    task = _task(db, appointment.id)
    if task is None:
        task = ImperialTask(source=SOURCE, external_id=str(appointment.id), category=PERSONAL_CATEGORY,
                            assigned_user_id=_owner_id(db), status="open", title=title)
        db.add(task)
        action = "created"
    else:
        action = "reopened" if task.status not in OPEN else "updated"
        task.status = "open"
    task.title = title
    task.details = "Confirmed appointment. It is also on the dashboard calendar."
    task.project_key = f"ro-{appointment.repair_order_id}"
    task.due_at = _aware(appointment.starts_at).astimezone(timezone.utc)
    task.updated_at = now
    db.flush()
    db.add(ImperialTaskAudit(task_id=task.id, actor=actor[:64], action=action,
                             event_id=f"appointment:{appointment.id}:confirmed:{now.isoformat()}"))
    return task


def close_reminder(db, appointment, actor: str, outcome: str):
    """outcome: 'completed' marks it done; anything else (cancelled, missed) cancels it."""
    if not _enabled():
        return None
    task = _task(db, appointment.id)
    if task is None or task.status not in OPEN:
        return task
    now = datetime.now(timezone.utc)
    task.status = "done_verified" if outcome == "completed" else "cancelled"
    task.updated_at = now
    db.add(ImperialTaskAudit(task_id=task.id, actor=actor[:64], action=outcome[:32],
                             event_id=f"appointment:{appointment.id}:{outcome}:{now.isoformat()}"))
    return task
