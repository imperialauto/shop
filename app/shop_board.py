"""The shop TV board: today's jobs, money in today against the daily goal, and the shop to-do list.

Read on a TV across the bay, so it carries first names and vehicles only: no phone numbers,
no addresses, no prices per job. The to-do list lives in imperial_tasks under its own
category (BOARD_CATEGORY), so no new table is needed and it stays off the owner's personal
to-do, the Master Task Tracker and Needs attention.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import business_settings
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.imperial_task_model import BOARD_CATEGORY, ImperialTask, ImperialTaskAudit
from app.models import Payment, RepairOrder, User

GOAL_KEY = "daily_sales_goal"
GOAL_DEFAULT = 500  # Jae's figure (Nov 21, 2026): $500 a day including parts
GOAL_MAX = 100000
OPEN_TODO = ("open", "in_progress", "blocked")
DONE = "done_verified"
MAX_JOBS = 12
MAX_TODOS = 14
MAX_DONE_SHOWN = 4

# Shop-floor words for each job stage, and the order the board lists them in.
STAGES = {
    "in_progress": ("Working on it", 0),
    "diagnosing": ("Diagnosing", 1),
    "approval_received": ("Approved, ready to start", 2),
    "intake": ("Checked in", 3),
    "waiting_parts": ("Waiting on parts", 4),
    "waiting_approval": ("Waiting on customer OK", 5),
    "complete": ("Done, ready for pickup", 6),
}


def now_local() -> datetime:
    return datetime.now(PHOENIX)


def daily_goal() -> int:
    try:
        value = int(float(business_settings.raw(GOAL_KEY) or ""))
    except (TypeError, ValueError):
        return GOAL_DEFAULT
    return value if 1 <= value <= GOAL_MAX else GOAL_DEFAULT


def _local(value):
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(PHOENIX)


def _who(customer) -> str:
    """First name and last initial: enough for the bay, not a full name on a wall."""
    if not customer:
        return ""
    first = (customer.first_name or "").strip()
    last = (customer.last_name or "").strip().rstrip("-").strip()
    if not first or first.lower().startswith("unknown"):
        return ""
    return f"{first} {last[0]}." if last and not last.lower().startswith("unknown") else first


def _job(order, *, time_label=None, mobile=False):
    label, rank = STAGES.get(order.status, (order.status.replace("_", " ").capitalize(), 9))
    tech = order.assigned_tech
    return {
        "id": order.id,
        "time": time_label,
        "mobile": mobile,
        "who": _who(order.customer),
        "vehicle": order.vehicle.label if order.vehicle else "Vehicle details not given yet",
        "stage": label,
        "stage_key": order.status,
        "rank": rank,
        "tech": ((tech.full_name or tech.username).split()[0] if tech and (tech.full_name or tech.username) else ""),
    }


def jobs_today(db: Session, now: datetime) -> list[dict]:
    """Today's booked visits first (by time), then every other open job by stage."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    visits = db.query(Appointment).filter(
        Appointment.status.in_(("tentative", "confirmed")),
        Appointment.starts_at >= start.astimezone(timezone.utc),
        Appointment.starts_at < (start + timedelta(days=1)).astimezone(timezone.utc),
    ).order_by(Appointment.starts_at).all()
    out, seen = [], set()
    for visit in visits:
        order = db.get(RepairOrder, visit.repair_order_id)
        if order is None or order.id in seen or order.status == "delivered":
            continue
        seen.add(order.id)
        out.append(_job(order, time_label=_local(visit.starts_at).strftime("%-I:%M %p"), mobile=visit.location == "mobile"))
    rest = db.query(RepairOrder).filter(RepairOrder.status.in_(tuple(STAGES))).all()
    rest = [_job(order) for order in rest if order.id not in seen]
    rest.sort(key=lambda job: (job["rank"], -job["id"]))
    return (out + rest)[:MAX_JOBS]


def sales_today(db: Session, now: datetime) -> dict:
    """Money recorded as received today (Phoenix day), against the daily goal."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    # paid_at is saved as UTC without a zone.
    since = start.astimezone(timezone.utc).replace(tzinfo=None)
    until = (start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    total = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.paid_at >= since, Payment.paid_at < until).scalar() or 0
    count = db.query(Payment).filter(Payment.paid_at >= since, Payment.paid_at < until).count()
    goal = daily_goal()
    total = round(float(total), 2)
    return {"total": total, "goal": goal, "count": count,
            "percent": max(0, min(100, int(total * 100 / goal))) if goal else 0,
            "left": round(max(goal - total, 0), 2), "hit": total >= goal}


def todos(db: Session, now: datetime) -> dict:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rows = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.status.in_(OPEN_TODO),
    ).order_by(ImperialTask.priority != "urgent", ImperialTask.id).all()
    done = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.status == DONE,
        ImperialTask.updated_at >= start,
    ).order_by(ImperialTask.updated_at.desc()).limit(MAX_DONE_SHOWN).all()
    item = lambda t: {"id": t.id, "title": t.title, "urgent": t.priority == "urgent"}  # noqa: E731
    return {"open": [item(t) for t in rows], "open_count": len(rows),
            "done": [item(t) for t in done]}


def snapshot(db: Session, user: User, now: datetime | None = None) -> dict:
    """Everything the board shows. Each part fails on its own, so one problem doesn't blank the TV."""
    from app.routes.imperial_tasks import enabled

    now = (now or now_local()).astimezone(PHOENIX)
    data = {"now": now, "jobs": None, "sales": None, "todos": None, "todos_on": enabled(),
            "show_money": user.role == "owner"}
    try:
        data["jobs"] = jobs_today(db, now)
    except SQLAlchemyError:
        db.rollback()
    if data["show_money"]:
        try:
            data["sales"] = sales_today(db, now)
        except SQLAlchemyError:
            db.rollback()
    if data["todos_on"]:
        try:
            data["todos"] = todos(db, now)
        except SQLAlchemyError:
            db.rollback()
    return data


def add_todo(db: Session, user: User, title: str, urgent: bool = False) -> ImperialTask:
    title = " ".join((title or "").split())
    if not 2 <= len(title) <= 200:
        raise ValueError("Type a to-do between 2 and 200 characters.")
    event_id = uuid4().hex
    task = ImperialTask(title=title, source="shop_board", external_id=event_id, category=BOARD_CATEGORY,
                        priority="urgent" if urgent else "normal", status="open")
    db.add(task)
    db.flush()
    db.add(ImperialTaskAudit(task_id=task.id, actor=f"user:{user.id}", action="created", event_id=event_id))
    db.commit()
    return task


def set_done(db: Session, user: User, task_id: int, done: bool) -> bool:
    """Tick off (or bring back) one board to-do. Other kinds of task can't be changed from here."""
    task = db.query(ImperialTask).filter_by(id=task_id, category=BOARD_CATEGORY).first()
    if task is None:
        return False
    task.status = DONE if done else "open"
    task.updated_at = now_local().astimezone(timezone.utc)
    db.add(ImperialTaskAudit(task_id=task.id, actor=f"user:{user.id}",
                             action="complete" if done else "reopen", event_id=uuid4().hex))
    db.commit()
    return True


def save_goal(db: Session, user: User, value: str) -> int:
    try:
        goal = int(float((value or "").replace("$", "").replace(",", "").strip()))
    except ValueError:
        raise ValueError("Enter the daily goal as a dollar amount, like 500.") from None
    if not 1 <= goal <= GOAL_MAX:
        raise ValueError("Enter a daily goal between $1 and $100,000.")
    if business_settings.save(db, GOAL_KEY, str(goal), f"owner:{user.id}"):
        db.commit()
        business_settings.clear_cache()
    return goal
