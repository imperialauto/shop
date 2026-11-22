"""A worker's home screen: their jobs, today's appointments, and unassigned work."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from app.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import next_action
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.routes.repair_orders import RO_STATUSES, workers

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
OPEN = [key for key, _ in RO_STATUSES if key not in ("delivered",)]


def _local(value):
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(PHOENIX)


@router.get("/my-day", response_class=HTMLResponse)
def my_day(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    now = datetime.now(PHOENIX)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    mine = db.query(RepairOrder).filter(RepairOrder.assigned_tech_id == user.id, RepairOrder.status.in_(OPEN)) \
        .order_by(RepairOrder.updated_at.desc()).all()
    # Finished work isn't waiting for anyone to pick it up.
    unassigned = db.query(RepairOrder).filter(RepairOrder.assigned_tech_id.is_(None),
                                              RepairOrder.status.in_([s for s in OPEN if s != "complete"])) \
        .order_by(RepairOrder.created_at.desc()).limit(15).all()
    today = db.query(Appointment).filter(Appointment.status.in_(("tentative", "confirmed")),
                                         Appointment.starts_at >= start.astimezone(timezone.utc),
                                         Appointment.starts_at < (start + timedelta(days=1)).astimezone(timezone.utc)) \
        .order_by(Appointment.starts_at).all()
    # All pages use the same RO next-step rules. My Day does not invent separate
    # state-transition buttons or recommend skipping customer approval.
    next_steps = {ro.id: next_action.for_job(db, ro, user.role) for ro in mine}
    listed_ids = {ro.id for ro in mine} | {ro.id for ro in unassigned}
    visit_by_ro = {}
    additional_visits = []
    for appointment in today:
        order = db.get(RepairOrder, appointment.repair_order_id)
        if order is None:
            continue
        visit = {"time": _local(appointment.starts_at).strftime("%-I:%M %p"),
                 "ro": order, "mobile": appointment.location == "mobile"}
        visit_by_ro.setdefault(order.id, visit)
        # A job already visible in Mine or Unassigned gets its appointment
        # time there; repeating it as a second action card causes confusion.
        if order.id not in listed_ids:
            additional_visits.append(visit)
    owner = _owner_day(db, user, now)
    return templates.TemplateResponse("my_day.html", {
        "owner": owner,
        "request": request, "user": user, "mine": mine, "unassigned": unassigned,
        "next_steps": next_steps, "visit_by_ro": visit_by_ro, "visits": additional_visits,
        "appointment_count": len(today),
        "read_only": request.app.state.read_only,
        "can_take": user.role != "tech" and user in workers(db),
        "today": now.strftime("%A, %B %-d"),
    })


def _owner_day(db, user, now):
    """For the owner: what needs him today, above the job lists. Read-only."""
    if user.role != "owner":
        return None
    from app import calendar_feed, task_board
    from app.routes.imperial_tasks import enabled
    day = {
        "plan": {"next": [], "overdue": [], "later": [], "backlog": []},
        "calendar": None, "tasks_on": enabled(), "load_error": False, "bills": [],
    }
    if day["tasks_on"]:
        try:
            day["plan"] = task_board.personal_plan(db, user.id, now)
        except Exception:
            db.rollback()
            day["load_error"] = True
    day["bills"] = bills_due_soon(db, now.date())
    try:
        events = calendar_feed.upcoming(now, days=1)
        if events is not None:
            day["calendar"] = [calendar_feed.describe(e) for e in events][:8]
    except Exception:
        day["calendar"] = None
    return day


BILL_HEADS_UP_DAYS = 3


def bills_due_soon(db, today):
    """Open bills past due or due within three days, shop first. Empty when the money tables aren't set up."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.finance_model import FinanceBill
    try:
        rows = (db.query(FinanceBill).filter(FinanceBill.status == "open",
                                             FinanceBill.due_date <= today + timedelta(days=BILL_HEADS_UP_DAYS))
                .order_by(FinanceBill.due_date, FinanceBill.name).all())
    except SQLAlchemyError:
        db.rollback()
        return []
    out = []
    for bill in sorted(rows, key=lambda b: (b.scope != "business", b.due_date, b.name)):
        days = (bill.due_date - today).days
        when = (f"{-days} day{'s' if days != -1 else ''} late" if days < 0 else
                "due today" if days == 0 else "due tomorrow" if days == 1 else f"due {bill.due_date.strftime('%a %b %-d')}")
        out.append({"name": bill.name, "amount": bill.amount_cents / 100, "when": when, "late": days < 0,
                    "personal": bill.scope == "personal"})
    return out
