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
    return templates.TemplateResponse("my_day.html", {
        "request": request, "user": user, "mine": mine, "unassigned": unassigned,
        "next_steps": next_steps, "visit_by_ro": visit_by_ro, "visits": additional_visits,
        "appointment_count": len(today),
        "can_take": user.role != "tech" and user in workers(db),
        "today": now.strftime("%A, %B %-d"),
    })
