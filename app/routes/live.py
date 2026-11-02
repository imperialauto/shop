"""TikTok LIVE overlay and its staff control page.

The overlay is public to anyone holding LIVE_OVERLAY_TOKEN, because TikTok LIVE
Studio's Link source cannot sign in. Its payload must never carry customer,
vehicle or repair order details. Booked value is quoted work, not collected money.
"""
import os
import secrets
from datetime import datetime, time, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from app.appointment_model import Appointment, BookingSlot
from app.appointments import AppointmentValidationError, AppointmentWindow, PHOENIX, validate_open_hours
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.routes.appointments import configured_hours

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

ACTIVE = ("tentative", "confirmed")
COUNTED = ("tentative", "confirmed", "completed")
OVERLAY_TIMES = 3
NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}


def require_token(token: str) -> None:
    expected = os.environ.get("LIVE_OVERLAY_TOKEN", "").strip()
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(404, "Not found")


def daily_goal() -> float:
    try:
        goal = float(os.environ.get("LIVE_DAILY_GOAL", "500"))
    except ValueError:
        return 500.0
    return goal if goal > 0 else 500.0


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def phoenix_label(value: datetime) -> str:
    local = utc(value).astimezone(PHOENIX)
    hour = local.hour % 12 or 12
    return f"{local:%a} {local:%b} {local.day}, {hour}:{local:%M} {local:%p}"


def phoenix_day(now: datetime) -> tuple[datetime, datetime]:
    day = now.astimezone(PHOENIX).date()
    start = datetime.combine(day, time.min, PHOENIX)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def open_slots(db: Session, now: datetime) -> list[BookingSlot]:
    slots = db.query(BookingSlot).filter(
        BookingSlot.removed_at.is_(None), BookingSlot.starts_at > now,
    ).order_by(BookingSlot.starts_at, BookingSlot.id).limit(200).all()
    if not slots:
        return []
    active = db.query(Appointment).filter(
        Appointment.status.in_(ACTIVE),
        Appointment.ends_at > now - timedelta(hours=4),
        Appointment.starts_at < slots[-1].ends_at + timedelta(hours=4),
    ).all()
    return [slot for slot in slots if not any(
        item.resource_id == slot.resource_id
        and utc(item.starts_at) - timedelta(minutes=item.travel_before_minutes or 0) < utc(slot.ends_at)
        and utc(slot.starts_at) < utc(item.ends_at) + timedelta(minutes=item.buffer_after_minutes or 0)
        for item in active
    )]


def booked_today(db: Session, now: datetime) -> list[Appointment]:
    start, end = phoenix_day(now)
    return db.query(Appointment).filter(
        Appointment.status.in_(COUNTED),
        Appointment.created_at >= start,
        Appointment.created_at < end,
    ).order_by(Appointment.created_at.desc()).all()


def snapshot(db: Session) -> dict:
    """The only data the public overlay receives."""
    now = datetime.now(timezone.utc)
    booked = booked_today(db, now)
    slots = open_slots(db, now)
    return {
        "booked_count": len(booked),
        "booked_value": float(sum(item.booked_value or 0 for item in booked)),
        "goal": daily_goal(),
        "open_count": len(slots),
        "open_times": [phoenix_label(slot.starts_at) for slot in slots[:OVERLAY_TIMES]],
    }


@router.get("/overlay/{token}", response_class=HTMLResponse)
def overlay_page(token: str, request: Request):
    require_token(token)
    return templates.TemplateResponse("live_overlay.html", {"request": request, "token": token}, headers=NO_STORE)


@router.get("/overlay/{token}/data")
def overlay_data(token: str, db: Session = Depends(get_db)):
    require_token(token)
    return JSONResponse(snapshot(db), headers=NO_STORE)


class SlotInput(BaseModel):
    resource_id: str = Field(min_length=1, max_length=80)
    starts_at: datetime
    ends_at: datetime

    @field_validator("resource_id")
    @classmethod
    def normalize_resource(cls, value):
        value = value.strip().casefold()
        if not value:
            raise ValueError("A technician or bay is required.")
        return value


@router.get("/control", response_class=HTMLResponse)
def control_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
    token = os.environ.get("LIVE_OVERLAY_TOKEN", "").strip()
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    overlay_url = f"{scheme}://{request.url.netloc}/live/overlay/{token}" if token else ""
    orders = db.query(RepairOrder).filter(RepairOrder.status != "delivered").order_by(RepairOrder.created_at.desc()).limit(100).all()
    return templates.TemplateResponse("live_control.html", {
        "request": request, "user": user, "overlay_url": overlay_url, "orders": orders,
    }, headers={"Cache-Control": "no-store"})


@router.get("/control/data")
def control_data(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    now = datetime.now(timezone.utc)
    booked = []
    for item in booked_today(db, now):
        order = db.get(RepairOrder, item.repair_order_id)
        customer = order.customer if order else None
        booked.append({
            "id": item.id, "repair_order_id": item.repair_order_id, "status": item.status,
            "customer": f"{customer.first_name} {customer.last_name}" if customer else "",
            "time": phoenix_label(item.starts_at), "booked_value": float(item.booked_value or 0),
        })
    return {
        **snapshot(db),
        "booked_today": booked,
        "slots": [{
            "id": slot.id, "resource_id": slot.resource_id, "label": phoenix_label(slot.starts_at),
            "starts_at": utc(slot.starts_at).isoformat(), "ends_at": utc(slot.ends_at).isoformat(),
        } for slot in open_slots(db, now)],
    }


@router.post("/slots", status_code=201)
def add_slot(payload: SlotInput, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    try:
        window = AppointmentWindow(payload.starts_at, payload.ends_at, payload.resource_id)
        validate_open_hours(window, configured_hours())
        if utc(window.starts_at) <= datetime.now(timezone.utc):
            raise AppointmentValidationError("Open times must be in the future.")
    except AppointmentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    starts_at, ends_at = utc(payload.starts_at), utc(payload.ends_at)
    duplicate = db.query(BookingSlot).filter(
        BookingSlot.removed_at.is_(None),
        BookingSlot.resource_id == payload.resource_id,
        BookingSlot.starts_at < ends_at,
        BookingSlot.ends_at > starts_at,
    ).first()
    if duplicate:
        raise HTTPException(409, "That time overlaps an open time already listed.")
    slot = BookingSlot(resource_id=payload.resource_id, starts_at=starts_at, ends_at=ends_at)
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return {"id": slot.id}


@router.post("/slots/{slot_id}/remove")
def remove_slot(slot_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    slot = db.get(BookingSlot, slot_id)
    if slot is None or slot.removed_at is not None:
        raise HTTPException(404, "Open time not found.")
    slot.removed_at = datetime.now(timezone.utc)
    db.commit()
    return {"id": slot.id}
