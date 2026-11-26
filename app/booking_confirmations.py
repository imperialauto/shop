"""Confirmation text for a website booking, to customers who chose text as the way to reach them.

Off by default (setting booking_confirmation_texts). When on, a booking queues one row here; the
messaging service picks it up on its next tick and texts it from the shop line. One text per
booking, never retried. It is skipped when the setting is off by then, the visit was cancelled or has
passed, the number is the owner's, or the customer has texted STOP (checked by the messaging service).
The wording is built when it goes out, so a moved visit shows its new time. No owner text goes with it.
"""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import approval_links, business_settings, launch_offer, public_links
from app.appointment_model import Appointment
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound
from app.models import RepairOrder
from app.public_site import BUSINESS

SETTING = "booking_confirmation_texts"
KIND = "booking_confirm"
PHOENIX = ZoneInfo("America/Phoenix")
LIVE = ("confirmed", "tentative")
SERVICE = re.compile(r"^Online booking: (.+?) on ", re.M)


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def key_for(appointment_id: int) -> str:
    return f"bookconf:{appointment_id}"


def queue(db: Session, appointment: Appointment, item: DispatchItem, contact: str) -> bool:
    """Call inside the booking's transaction, after the appointment and queue item exist.
    Returns True when a confirmation text is waiting to go out."""
    if not enabled() or contact != "text":
        return False
    phone = approval_links.mobile(item.customer_phone)
    if phone is None or phone == approval_links.mobile(OWNER_PHONE):
        return False
    db.add(DispatchOutbound(dedupe_key=key_for(appointment.id), kind=KIND, item_id=item.id, status="queued"))
    db.add(DispatchAudit(item_id=item.id, actor="website", action="booking_confirm_queued",
                         detail=f"appointment {appointment.id}"))
    return True


def _utc(value: datetime) -> datetime:
    # SQLite hands times back without a zone; they were saved in UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def when(starts_at: datetime) -> str:
    local = _utc(starts_at).astimezone(PHOENIX)
    clock = local.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return f"{local:%a, %b} {local.day} at {clock}"


def message_for(order: RepairOrder, appointment: Appointment) -> str:
    found = SERVICE.search(order.concern or "")
    service = found.group(1) if found else "your visit"
    where = ("We'll come to the address you gave." if appointment.location == "mobile"
             else f"We're at {BUSINESS['street']}, {BUSINESS['city']}.")
    text = (f"Hi {approval_links.greeting_for(order)}, you're booked at {BUSINESS['name']} for {service} on "
            f"{when(appointment.starts_at)}. {where} Your reference is WEB-{order.id}.")
    if launch_offer.MARK in (order.concern or ""):
        percent = re.search(r"Launch offer: (\d+)% off labor", order.concern)
        text += f" You got our launch offer: {percent.group(1) if percent else 20}% off labor."
    if order.public_token:
        text += f" Check on it here: {public_links.public_url('/status/' + order.public_token)}"
    return text + " Reply here with any questions. Reply STOP to opt out. Jae"


def _skip(db: Session, row: DispatchOutbound, now: datetime, why: str) -> None:
    row.status, row.updated_at = "skipped", now
    db.add(DispatchAudit(item_id=row.item_id, actor="system", action="booking_confirm_skipped", detail=why[:255]))


def due(db: Session, now: datetime | None = None) -> list[dict]:
    """Hands each waiting confirmation to the messaging service once, as {key, phone, text}."""
    now = now or datetime.now(timezone.utc)
    rows = (db.query(DispatchOutbound).filter_by(kind=KIND, status="queued")
            .order_by(DispatchOutbound.id).with_for_update(skip_locked=True).limit(10).all())
    out = []
    for row in rows:
        item = db.get(DispatchItem, row.item_id) if row.item_id else None
        appointment_id = row.dedupe_key.split(":", 1)[-1]
        appointment = db.get(Appointment, int(appointment_id)) if appointment_id.isdigit() else None
        order = db.get(RepairOrder, appointment.repair_order_id) if appointment else None
        phone = approval_links.mobile(item.customer_phone) if item else None
        if not enabled():
            _skip(db, row, now, "setting off")
        elif item is None or order is None or order.customer is None or phone is None:
            _skip(db, row, now, "booking or phone gone")
        elif appointment.status not in LIVE or _utc(appointment.starts_at) <= now:
            _skip(db, row, now, f"visit {appointment.status}")
        elif phone == approval_links.mobile(OWNER_PHONE):
            _skip(db, row, now, "owner number")
        else:
            row.status, row.updated_at = "reserved", now
            out.append({"key": row.dedupe_key, "phone": phone, "text": message_for(order, appointment)})
    db.commit()
    return out
