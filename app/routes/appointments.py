"""Authenticated appointment API. Import router after the additive DB migration.

Requires explicit SHOP_HOURS_JSON (weekday index 0=Monday) to allow bookings.
Uses a PostgreSQL transaction-scoped advisory lock to serialize bookings per resource.
"""
import json
import os
from datetime import datetime, time, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.appointment_model import Appointment
from app.appointments import AppointmentWindow, AppointmentValidationError, validate_slot

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), '..', 'templates'))

@router.get('/calendar', response_class=HTMLResponse)
def calendar_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse('calendar.html', {'request': request, 'user': user})

class AppointmentInput(BaseModel):
    repair_order_id: int = Field(gt=0)
    resource_id: str = Field(min_length=1, max_length=80)
    starts_at: datetime
    ends_at: datetime

def configured_hours():
    raw = os.environ.get("SHOP_HOURS_JSON")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {int(day): (time.fromisoformat(pair[0]), time.fromisoformat(pair[1])) for day, pair in data.items() if int(day) in range(7)}
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        raise HTTPException(503, "Shop hours configuration is invalid.") from exc

@router.get("/")
def list_appointments(request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    return [
        {"id": a.id, "repair_order_id": a.repair_order_id, "resource_id": a.resource_id,
         "starts_at": a.starts_at, "ends_at": a.ends_at, "status": a.status}
        for a in db.query(Appointment).order_by(Appointment.starts_at).limit(250).all()
    ]

@router.post("/", status_code=201)
def book_appointment(payload: AppointmentInput, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    if db.get(RepairOrder, payload.repair_order_id) is None:
        raise HTTPException(404, "Repair order not found.")
    try:
        candidate = AppointmentWindow(payload.starts_at, payload.ends_at, payload.resource_id)
        validate_slot(candidate, [], configured_hours())
    except AppointmentValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    try:
        # PostgreSQL lock is transaction-scoped, released on commit or rollback.
        # Never enable this endpoint on unsupported database backends.
        if db.bind.dialect.name != "postgresql":
            raise HTTPException(503, "Concurrent booking requires PostgreSQL.")
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:resource))"), {"resource": payload.resource_id})
        conflicting = db.query(Appointment).filter(
            Appointment.resource_id == payload.resource_id,
            Appointment.status.in_(("tentative", "confirmed")),
            Appointment.starts_at < payload.ends_at.astimezone(timezone.utc),
            Appointment.ends_at > payload.starts_at.astimezone(timezone.utc),
        ).first()
        if conflicting:
            raise HTTPException(409, "Resource is already booked for this time.")
        item = Appointment(
            repair_order_id=payload.repair_order_id, resource_id=payload.resource_id,
            starts_at=payload.starts_at.astimezone(timezone.utc), ends_at=payload.ends_at.astimezone(timezone.utc), status="tentative",
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        return {"id": item.id, "status": item.status, "repair_order_id": item.repair_order_id}
    except Exception:
        db.rollback()
        raise


@router.post("/{appointment_id}/confirm")
def confirm_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    try:
        if db.bind.dialect.name != "postgresql":
            raise HTTPException(503, "Concurrent booking requires PostgreSQL.")
        appointment = db.get(Appointment, appointment_id)
        if appointment is None:
            raise HTTPException(404, "Appointment not found.")
        if appointment.status != "tentative":
            raise HTTPException(409, "Only tentative appointments can be confirmed.")
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:resource))"), {"resource": appointment.resource_id})
        conflict = db.query(Appointment).filter(
            Appointment.id != appointment.id,
            Appointment.resource_id == appointment.resource_id,
            Appointment.status.in_(("tentative", "confirmed")),
            Appointment.starts_at < appointment.ends_at,
            Appointment.ends_at > appointment.starts_at,
        ).first()
        if conflict:
            raise HTTPException(409, "Resource has a conflicting appointment.")
        appointment.status = "confirmed"
        db.commit()
        return {"id": appointment.id, "status": appointment.status}
    except Exception:
        db.rollback()
        raise


@router.post("/{appointment_id}/cancel")
def cancel_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(404, "Appointment not found.")
    if appointment.status in ("completed", "cancelled"):
        raise HTTPException(409, "Appointment cannot be cancelled from its current state.")
    appointment.status = "cancelled"
    db.commit()
    return {"id": appointment.id, "status": appointment.status}


@router.post("/{appointment_id}/complete")
def complete_appointment(appointment_id: int, request: Request, db: Session = Depends(get_db)):
    get_current_user(request, db)
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(404, "Appointment not found.")
    if appointment.status != "confirmed":
        raise HTTPException(409, "Only confirmed appointments can be completed.")
    appointment.status = "completed"
    db.commit()
    return {"id": appointment.id, "status": appointment.status}
