"""A worker's home screen: their jobs, today's appointments, and unassigned work."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.routes.repair_orders import RO_STATUSES

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
OPEN = [key for key, _ in RO_STATUSES if key not in ("delivered",)]
TECH_STEPS = [(k, label) for k, label in RO_STATUSES if k != "delivered"]


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
    unassigned = db.query(RepairOrder).filter(RepairOrder.assigned_tech_id.is_(None), RepairOrder.status.in_(OPEN)) \
        .order_by(RepairOrder.created_at.desc()).limit(15).all()
    today = db.query(Appointment).filter(Appointment.status.in_(("tentative", "confirmed")),
                                         Appointment.starts_at >= start.astimezone(timezone.utc),
                                         Appointment.starts_at < (start + timedelta(days=1)).astimezone(timezone.utc)) \
        .order_by(Appointment.starts_at).all()
    visits = [{"time": _local(a.starts_at).strftime("%-I:%M %p"), "ro": db.get(RepairOrder, a.repair_order_id),
               "mobile": a.location == "mobile"} for a in today]
    return templates.TemplateResponse("my_day.html", {"request": request, "user": user, "mine": mine, "unassigned": unassigned,
                                      "visits": visits, "steps": TECH_STEPS, "today": now.strftime("%A, %B %-d")})
