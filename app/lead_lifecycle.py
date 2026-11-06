"""Feature-gated intake-to-outcome lifecycle for service opportunities.

A communication item may be completed without resolving a lead. Only a
verified confirmed appointment or a documented explicit customer decision
can end follow-up. This module has no external network or SMS effects.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.lead_model import (
    ACTIVE_LEAD_STATES, ServiceLead, ServiceLeadAudit, utc_now,
)
from app.models import RepairOrder
from app.appointment_model import Appointment

AZ = ZoneInfo("America/Phoenix")
FOLLOW_UP_INTERVAL = timedelta(minutes=30)
_SERVICE_WORDS = re.compile(
    r"\b(repair|replace|replacement|mechanic|diagnos\w*|service|oil change|"
    r"starter|alternator|battery|brakes?|radiator|transmission|clutch|"
    r"windshield|air conditioning|a/c|won'?t start|tire|tow\w*|"
    r"inspection|check engine|not starting)\b", re.I
)


def enabled() -> bool:
    return os.getenv("SERVICE_LEADS_ENABLED", "").strip().lower() == "true"


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _service_request(result, message, lead) -> bool:
    if lead is not None:
        return True
    if result.category in ("repair", "towing", "pricing", "scheduling"):
        return True
    return bool(_SERVICE_WORDS.search(message.body or ""))


def _audit(db: Session, lead: ServiceLead, action: str, actor: str,
           before: str | None = None, evidence: str | None = None) -> None:
    db.add(ServiceLeadAudit(
        lead_id=lead.id, action=action, actor=actor,
        from_status=before, to_status=lead.status, evidence_ref=evidence,
    ))


def intake_from_quo(db: Session, message, result, dispatch_item, now: datetime | None = None):
    """Called inside the existing atomic dispatcher transaction after dedupe."""
    if not enabled():
        return None
    now = aware(now or utc_now())
    lead = db.query(ServiceLead).filter(
        ServiceLead.source == "quo",
        ServiceLead.external_thread_id == message.conversationId,
        ServiceLead.status.in_(ACTIVE_LEAD_STATES),
    ).order_by(ServiceLead.id.desc()).first()
    if not _service_request(result, message, lead):
        return None
    new = lead is None
    if new:
        lead = ServiceLead(
            source="quo", external_thread_id=message.conversationId,
            customer_phone=message.phone, status="new",
            created_at=now,
        )
        db.add(lead)
    else:
        # A reply to a pending estimate changes the next staff action, not the
        # customer's booking status. Never close because a message was sent.
        if lead.status == "awaiting_customer":
            lead.status = "new"
    if dispatch_item.customer_id:
        lead.customer_id = dispatch_item.customer_id
    if dispatch_item.vehicle_id:
        lead.vehicle_id = dispatch_item.vehicle_id
    if message.phone:
        lead.customer_phone = message.phone
    lead.last_dispatch_item_id = dispatch_item.id
    lead.last_event_id = message.messageId
    if result.category != "general" or new:
        lead.category = result.category
    if result.urgency == "safety" or (result.urgency == "urgent" and lead.urgency != "safety"):
        lead.urgency = result.urgency
    if message.body.strip():
        lead.summary = message.body.strip()[:500]
    lead.next_action = (dispatch_item.proposed_action or "Review the request and contact the customer.")[:1000]
    lead.next_action_due_at = now if result.urgency != "normal" else now + FOLLOW_UP_INTERVAL
    lead.last_customer_activity_at = now
    lead.updated_at = now
    db.flush()
    _audit(db, lead, "lead_opened" if new else "customer_reply", "system",
           evidence=f"quo:{message.messageId}"[:128])
    return lead


def confirm_booking(db: Session, lead_id: int, appointment_id: int,
                    actor: str, now: datetime | None = None) -> ServiceLead:
    """Only a real confirmed appointment linked to the correct customer wins."""
    lead = db.get(ServiceLead, lead_id)
    appointment = db.get(Appointment, appointment_id)
    if lead is None or appointment is None or lead.status not in ACTIVE_LEAD_STATES:
        raise ValueError("Active lead and appointment required.")
    if appointment.status != "confirmed":
        raise ValueError("Booking must be confirmed, not tentative.")
    order = db.get(RepairOrder, appointment.repair_order_id)
    if order is None or lead.customer_id is None or lead.customer_id != order.customer_id:
        raise ValueError("Confirmed booking must belong to the lead's matched customer.")
    if lead.vehicle_id is not None and lead.vehicle_id != order.vehicle_id:
        raise ValueError("Booking belongs to another vehicle.")
    before = lead.status
    lead.status = "booked"
    lead.repair_order_id = order.id
    lead.appointment_id = appointment.id
    lead.vehicle_id = order.vehicle_id
    lead.resolved_at = aware(now or utc_now())
    lead.updated_at = lead.resolved_at
    lead.closure_reason = "confirmed_appointment"
    lead.closure_evidence_ref = f"appointment:{appointment.id}"
    _audit(db, lead, "booked", actor, before, lead.closure_evidence_ref)
    db.flush()
    return lead


def record_customer_decline(db: Session, lead_id: int, evidence_ref: str,
                            actor: str, explicit_customer_decline: bool,
                            now: datetime | None = None) -> ServiceLead:
    """No NLP guess or staff's 'NO' to a draft can reject the service lead."""
    if not explicit_customer_decline or not actor.strip() or not evidence_ref.strip():
        raise ValueError("Explicit customer decline and source evidence are required.")
    lead = db.get(ServiceLead, lead_id)
    if lead is None or lead.status not in ACTIVE_LEAD_STATES:
        raise ValueError("Active lead required.")
    before = lead.status
    lead.status = "declined"
    lead.closure_reason = "customer_declined_service"
    lead.closure_evidence_ref = evidence_ref.strip()[:128]
    lead.resolved_at = aware(now or utc_now())
    lead.updated_at = lead.resolved_at
    _audit(db, lead, "declined", actor, before, lead.closure_evidence_ref)
    db.flush()
    return lead


def reopen_cancelled_booking(db: Session, appointment_id: int,
                             actor: str, now: datetime | None = None) -> int:
    """When a booked visit cancels, the unfulfilled service needs a new action."""
    appointment = db.get(Appointment, appointment_id)
    if appointment is None or appointment.status != "cancelled":
        return 0
    now = aware(now or utc_now())
    reopened = 0
    for lead in db.query(ServiceLead).filter(
        ServiceLead.appointment_id == appointment_id, ServiceLead.status == "booked",
    ).all():
        lead.status = "scheduling"
        lead.appointment_id = None
        lead.resolved_at = None
        lead.closure_reason = None
        lead.closure_evidence_ref = None
        lead.next_action = "Cancelled appointment: offer a replacement slot or confirm decline."
        lead.next_action_due_at = now
        lead.updated_at = now
        _audit(db, lead, "booking_cancelled", actor, "booked", f"appointment:{appointment_id}")
        reopened += 1
    db.flush()
    return reopened


def due_lead_digest(db: Session, now: datetime) -> tuple[str, str] | None:
    """At most one owner summary per 30-minute window; no customer text.

    All unresolved records remain eligible regardless of age (including day 4+).
    Urgent leads also qualify outside regular hours.
    """
    if not enabled():
        return None
    now = aware(now)
    local = now.astimezone(AZ)
    open_hours = ((local.weekday() < 5 and 11 <= local.hour < 19)
                  or (local.weekday() == 5 and 12 <= local.hour < 17))
    rows = db.query(ServiceLead).filter(
        ServiceLead.status.in_(ACTIVE_LEAD_STATES),
        ServiceLead.next_action_due_at <= now,
    ).all()
    due = [item for item in rows
           if aware(item.next_action_due_at) <= now and
           (open_hours or item.urgency in ("urgent", "safety"))]
    if not due:
        return None
    due.sort(key=lambda x: (
        0 if x.urgency == "safety" else 1 if x.urgency == "urgent" else 2,
        aware(x.next_action_due_at),
    ))
    half = "00" if local.minute < 30 else "30"
    key = f"leads:{local:%Y%m%d%H}{half}"
    preview = "; ".join(
        f"#{lead.id} {lead.category} ({lead.status}) {lead.customer_phone or 'unknown'}"
        for lead in due[:3]
    )
    more = f"; +{len(due) - 3} more" if len(due) > 3 else ""
    message = (f"{len(due)} service leads still need action. "
               f"These remain open until booked or customer declines. "
               f"{preview}{more}. Open Imperial to assign/resolve.")
    return key, message
