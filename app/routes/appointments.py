"""Authenticated appointment API. Import router after the additive DB migration.

Requires explicit SHOP_HOURS_JSON (weekday index 0=Monday) to allow bookings.
Uses a PostgreSQL transaction-scoped advisory lock to serialize bookings per resource.
"""
import json
import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from app.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from app import appointment_followup, business_settings, lead_lifecycle, website_alerts
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.dispatch_model import DispatchAudit
from app.appointment_model import Appointment
from app.appointments import AppointmentWindow, AppointmentValidationError, overlaps, validate_slot

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), '..', 'templates'))

@router.get('/calendar', response_class=HTMLResponse)
def calendar_page(request: Request, ro: str = "", day: str = "", length: int = 1, bay: str = "",
                  where: str = "shop", travel: int = 30, booked: str = "", db: Session = Depends(get_db)):
    """The Calendar tab. With ?ro=ID (the job page's Schedule button) it opens the time picker for that job."""
    user = get_current_user(request, db)
    from app import calendar_view
    now = datetime.now(timezone.utc)
    may_change = user.role != "tech"  # techs can look; the permission gate refuses their changes
    may_mark_missed = user.role in ('owner', 'admin', 'office')
    ro = ro.strip()
    context = {'request': request, 'user': user, 'may_change': may_change,
               'sections': calendar_view.calendar_sections(db, now, may_change, may_mark_missed),
               'schedule': None, 'schedule_problem': '', 'ro_query': ro[:12], 'booked': None}
    if ro and may_change:
        order = db.get(RepairOrder, int(ro)) if ro.isdigit() and len(ro) < 10 else None
        if order is None:
            context['schedule_problem'] = f"There's no job #{ro[:12]}. Check the number on the job page."
        else:
            try:
                hours, buffer = configured_hours(), shop_buffer_minutes()
            except HTTPException as exc:
                hours, buffer = {}, 0
                context['schedule_problem'] = str(exc.detail)
            if not hours and not context['schedule_problem']:
                context['schedule_problem'] = "Shop hours aren't set, so no times can be booked. Set them in Settings first."
            if hours:
                context['schedule'] = calendar_view.schedule_view(
                    db, order, now, hours, buffer, day=day, length=length, bay=bay, where=where, travel=travel)
    if booked.isdigit() and len(booked) < 10:
        item = db.get(Appointment, int(booked))
        if item is not None:
            context['booked'] = {'id': item.id, 'job_id': item.repair_order_id, 'status': item.status,
                                 'when': calendar_view.local(item.starts_at).strftime("%a %b %-d, %-I:%M %p")}
    return templates.TemplateResponse('calendar.html', context)

MAX_TRAVEL_MINUTES = 240
ACTIVE_STATUSES = ("tentative", "confirmed")


def shop_buffer_minutes() -> int:
    """One hour after shop appointments unless SHOP_BUFFER_MINUTES says otherwise."""
    raw = business_settings.raw("shop_buffer_minutes") or "60"
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(503, "Shop buffer configuration is invalid.") from exc
    if not 0 <= value <= MAX_TRAVEL_MINUTES:
        raise HTTPException(503, "Shop buffer configuration is invalid.")
    return value


def _utc(value: datetime) -> datetime:
    """Stored times are UTC; a driver that drops the zone (SQLite) gets it back here."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def as_window(item: Appointment) -> AppointmentWindow:
    return AppointmentWindow(_utc(item.starts_at), _utc(item.ends_at), item.resource_id,
                             item.travel_before_minutes or 0, item.buffer_after_minutes or 0)


def find_conflict(db: Session, candidate: AppointmentWindow, exclude_id: int | None = None):
    """Run inside the advisory lock. Widened query, then exact blocked-time check."""
    margin = timedelta(minutes=2 * MAX_TRAVEL_MINUTES)
    query = db.query(Appointment).filter(
        Appointment.resource_id == candidate.resource_id,
        Appointment.status.in_(ACTIVE_STATUSES),
        Appointment.starts_at < candidate.blocked_until + margin,
        Appointment.ends_at > candidate.blocked_from - margin,
    )
    if exclude_id is not None:
        query = query.filter(Appointment.id != exclude_id)
    return next((item for item in query.all() if overlaps(candidate, as_window(item))), None)


class AppointmentInput(BaseModel):
    repair_order_id: int = Field(gt=0)
    resource_id: str = Field(min_length=1, max_length=80)
    starts_at: datetime
    ends_at: datetime
    booked_value: Decimal = Field(default=Decimal("0"), ge=0, le=Decimal("99999999.99"), decimal_places=2)
    location: Literal["shop", "mobile"] = "shop"
    travel_minutes: int = Field(default=0, ge=0, le=MAX_TRAVEL_MINUTES)

    @model_validator(mode="after")
    def mobile_needs_travel(self):
        if self.location == "mobile" and self.travel_minutes < 1:
            raise ValueError("Mobile appointments need travel time.")
        if self.location == "shop" and self.travel_minutes:
            raise ValueError("Shop appointments do not have travel time.")
        return self

    @field_validator("resource_id")
    @classmethod
    def normalize_resource(cls, value):
        value = value.strip().casefold()
        if not value:
            raise ValueError("A technician or bay is required.")
        return value

def configured_hours():
    raw = business_settings.raw("shop_hours")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("Expected weekday-to-hours mapping")
        hours = {}
        for day, pair in data.items():
            index = int(day)
            if index not in range(7):
                raise ValueError("Weekday outside 0-6")
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Expected exactly opening and closing times")
            opens, closes = time.fromisoformat(pair[0]), time.fromisoformat(pair[1])
            if opens >= closes or opens.tzinfo is not None or closes.tzinfo is not None:
                raise ValueError("Invalid opening interval")
            hours[index] = (opens, closes)
        return hours
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise HTTPException(503, "Shop hours configuration is invalid.") from exc

@router.get("/")
def list_appointments(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    missed = {detail for (detail,) in db.query(DispatchAudit.detail).filter_by(action="followup_missed_visit")}
    return [
        {"id": a.id, "repair_order_id": a.repair_order_id, "resource_id": a.resource_id,
         "starts_at": a.starts_at, "ends_at": a.ends_at, "status": a.status,
         "booked_value": float(a.booked_value or 0), "location": a.location, "missed": str(a.id) in missed,
         "travel_before_minutes": a.travel_before_minutes, "buffer_after_minutes": a.buffer_after_minutes}
        for a in db.query(Appointment).order_by(Appointment.starts_at).limit(250).all()
    ]

@router.post("/", status_code=201)
def book_appointment(payload: AppointmentInput, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    return create_appointment(db, payload)


def create_appointment(db: Session, payload: AppointmentInput, before_commit=None):
    """Book a tentative appointment. Shared by staff pages and the assistant API.

    before_commit(item) runs inside the booking transaction, after the row is flushed."""
    if db.get(RepairOrder, payload.repair_order_id) is None:
        raise HTTPException(404, "Repair order not found.")
    try:
        buffer_minutes = shop_buffer_minutes() if payload.location == "shop" else 0
        candidate = AppointmentWindow(payload.starts_at, payload.ends_at, payload.resource_id,
                                      payload.travel_minutes, buffer_minutes)
        validate_slot(candidate, [], configured_hours())
        if candidate.starts_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise AppointmentValidationError("Appointments must start in the future.")
    except AppointmentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        # PostgreSQL lock is transaction-scoped, released on commit or rollback.
        # Never enable this endpoint on unsupported database backends.
        if db.bind.dialect.name != "postgresql":
            raise HTTPException(503, "Concurrent booking requires PostgreSQL.")
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:resource))"), {"resource": payload.resource_id})
        conflicting = find_conflict(db, candidate)
        if conflicting:
            raise HTTPException(409, "Resource is already booked for this time.")
        item = Appointment(
            repair_order_id=payload.repair_order_id, resource_id=payload.resource_id,
            starts_at=payload.starts_at.astimezone(timezone.utc), ends_at=payload.ends_at.astimezone(timezone.utc), status="tentative",
            booked_value=payload.booked_value, location=payload.location,
            travel_before_minutes=payload.travel_minutes, buffer_after_minutes=buffer_minutes,
        )
        db.add(item)
        if before_commit is not None:
            db.flush()
            before_commit(item)
        db.commit()
        db.refresh(item)
        return {"id": item.id, "status": item.status, "repair_order_id": item.repair_order_id}
    except Exception:
        db.rollback()
        raise


@router.post("/{appointment_id}/confirm")
def confirm_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return confirm(db, appointment_id, f"user:{user.id}", user.username)


def confirm(db: Session, appointment_id: int, actor: str, closed_by: str, before_commit=None):
    """Tentative -> confirmed. Shared by the calendar page and the assistant API.

    A confirmed appointment stays on the calendar and gets the owner's reminder task."""
    try:
        if db.bind.dialect.name != "postgresql":
            raise HTTPException(503, "Concurrent booking requires PostgreSQL.")
        appointment = db.query(Appointment).filter(Appointment.id == appointment_id).with_for_update().first()
        if appointment is None:
            raise HTTPException(404, "Appointment not found.")
        if appointment.status != "tentative":
            raise HTTPException(409, "Only tentative appointments can be confirmed.")
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:resource))"), {"resource": appointment.resource_id})
        conflict = find_conflict(db, as_window(appointment), exclude_id=appointment.id)
        if conflict:
            raise HTTPException(409, "Resource has a conflicting appointment.")
        appointment.status = "confirmed"
        lead_lifecycle.confirm_website_lead_for_order(db, appointment, actor)
        order = db.get(RepairOrder, appointment.repair_order_id)
        if order is not None:
            website_alerts.close_for_job(db, order, closed_by, "appointment confirmed")
        appointment_followup.add_reminder(db, appointment, actor)
        if before_commit is not None:
            before_commit(appointment)
        db.commit()
        return {"id": appointment.id, "status": appointment.status}
    except Exception:
        db.rollback()
        raise


@router.post("/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return close(db, appointment_id, "cancelled", f"user:{user.id}")


@router.post("/{appointment_id}/complete")
def complete_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return close(db, appointment_id, "completed", f"user:{user.id}")


def close(db: Session, appointment_id: int, outcome: str, actor: str, before_commit=None):
    """Cancel or complete a visit. Shared by the calendar page and the assistant API; sends nothing to the customer."""
    appointment = db.query(Appointment).filter(Appointment.id == appointment_id).with_for_update().first()
    if appointment is None:
        raise HTTPException(404, "Appointment not found.")
    if outcome == "cancelled":
        if appointment.status in ("completed", "cancelled"):
            raise HTTPException(409, "Appointment cannot be cancelled from its current state.")
    elif outcome == "completed":
        if appointment.status != "confirmed":
            raise HTTPException(409, "Only confirmed appointments can be completed.")
    else:
        raise HTTPException(422, "Unknown change.")
    appointment.status = outcome
    appointment_followup.close_reminder(db, appointment, actor, outcome)
    if outcome == "cancelled" and lead_lifecycle.enabled():
        lead_lifecycle.reopen_cancelled_booking(db, appointment.id, actor)
    if before_commit is not None:
        before_commit(appointment)
    db.commit()
    return {"id": appointment.id, "status": appointment.status}


@router.post("/{appointment_id}/missed")
def missed_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role not in ("owner", "admin", "office"):
        raise HTTPException(403, "Only office staff can record missed visits.")
    appointment = db.query(Appointment).filter_by(id=appointment_id).with_for_update().first()
    if appointment is None:
        raise HTTPException(404, "Appointment not found.")
    from app.customer_followups import utc
    if appointment.status != "confirmed" or utc(appointment.ends_at) > datetime.now(timezone.utc):
        raise HTTPException(409, "Only a past confirmed appointment can be marked missed.")
    appointment.status = "cancelled"
    db.add(DispatchAudit(actor=user.username, action="followup_missed_visit", detail=str(appointment.id)))
    appointment_followup.close_reminder(db, appointment, f"user:{user.id}", "missed")
    if lead_lifecycle.enabled():
        lead_lifecycle.reopen_cancelled_booking(db, appointment.id, f"user:{user.id}")
    db.commit()
    return {"id": appointment.id, "status": appointment.status, "missed": True}

