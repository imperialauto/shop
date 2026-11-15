"""Customer-facing reminders. Select from recorded shop activity and reserve each send once."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy import String, cast, or_, text

from app import approval_links, business_settings
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.config import OWNER_PHONE
from app.dispatch import ARRIVAL, REASONS, SAFETY
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound, OPEN_STATUSES
from app.models import Communication, Customer, Invoice, Payment, RepairOrder
from app.lead_model import ServiceLead

KIND = "cust_followup"
FOLLOWUP_WINDOW = timedelta(days=30)
INBOUND, OUTBOUND = ("inbound", "outbound")
SERVICE_CATEGORIES = {"repair", "pricing", "scheduling", "towing"}
MESSAGES = {
    "unanswered": "Hi, this is Imperial Auto Care. Thanks for reaching out. Do you still need help with your vehicle? Reply here and we can help with the next step.",
    "waiting": "Hi, this is Imperial Auto Care following up. Do you still need help with your vehicle? Reply here and we can help with the next step.",
    "estimate": "Hi, this is Imperial Auto Care checking in on the estimate we sent. Do you have any questions, or would you like help finding a time to bring the vehicle in?",
    "reactivate": "Hi, this is Imperial Auto Care checking back on your service request. If you still need help, reply here and we can look at available times.",
    "missed_visit": "Hi, this is Imperial Auto Care checking in about your visit. If you still need service, reply here and we can find another time that works.",
}
OPT_OUT = re.compile(r"^\s*(?:stop(?:\s+all)?|stopall|unsubscribe|cancel|end|quit|opt\s*out|remove me)\s*[.!]?\s*$|\b(?:do not|don't|please stop)\s+(?:text|contact|messag)|\b(?:no thanks|not interested|already (?:fixed|repaired|handled|taken care of)|went (?:with|to) another)\b", re.I)
POSSIBLE_DROP_BY = re.compile(r"\b(?:i(?:['’]ll| will| can)|we(?:['’]ll| will| can)|going to|plan to|planning to)\s+(?:stop|drop|come|swing)\s+(?:by|in|it off|the car off)\b", re.I)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def enabled():
    return (business_settings.raw("automatic_customer_followups") or "").strip().lower() == "true"


def _recent_sms(db, customer_id, since):
    return db.query(Communication).filter(
        Communication.customer_id == customer_id, Communication.channel == "sms",
        Communication.direction.in_((INBOUND, OUTBOUND)),
        Communication.created_at >= since.replace(tzinfo=None),
    ).order_by(Communication.created_at.desc(), Communication.id.desc()).all()


def _history_blocks(db, customer_id):
    inbound = db.query(Communication.body).filter_by(customer_id=customer_id, channel="sms", direction=INBOUND)
    return any(OPT_OUT.search(body or "") for (body,) in inbound)


def estimate_order(db, orders, message):
    text = message.body or ""
    if message.direction != OUTBOUND or "your estimate" not in text.casefold():
        return None
    for order in orders:
        if order.public_token and f"/status/{order.public_token}" in text:
            link = db.query(DispatchItem.id).filter_by(quo_conversation_id=approval_links.conversation_key(order.id)).first()
            if link:
                return order
    return None


def _marked(db, action, reference):
    return db.query(DispatchAudit).filter_by(action=action, detail=str(reference)).order_by(DispatchAudit.id.desc()).first()


def offsite_estimate_at(db, order_id):
    mark = db.query(DispatchAudit).filter(
        DispatchAudit.action.in_(("followup_offsite_estimate", "followup_vehicle_present")),
        DispatchAudit.detail == str(order_id),
    ).order_by(DispatchAudit.id.desc()).first()
    return utc(mark.created_at) if mark and mark.action == "followup_offsite_estimate" else None


def _activity(db, customer, orders, now):
    sms = _recent_sms(db, customer.id, now - FOLLOWUP_WINDOW)
    latest = sms[0] if sms else None
    anchor = next((message for message in sms if message.body not in MESSAGES.values()), None)
    items = db.query(DispatchItem).filter_by(customer_id=customer.id).order_by(DispatchItem.updated_at.desc()).all()
    item = next((row for row in items if row.category in SERVICE_CATEGORIES or POSSIBLE_DROP_BY.search(row.summary or "")), None)
    marked_visit = db.query(Appointment.id).join(DispatchAudit, DispatchAudit.detail == cast(Appointment.id, String)).filter(
        Appointment.repair_order_id.in_([order.id for order in orders]),
        DispatchAudit.action == "followup_missed_visit",
    ).first()
    if any(row.status in OPEN_STATUSES and (row.urgency == "safety" or row.category == "arrival" or
           set(json.loads(row.approval_reasons or "[]")) & {"high_risk", "refund"}) for row in items):
        return None
    if anchor and (SAFETY.search(anchor.body or "") or anchor.direction == INBOUND and ARRIVAL.search(anchor.body or "") or
                   any(REASONS[name].search(anchor.body or "") for name in ("refund", "high_risk"))):
        return None
    recent_item = item and utc(item.updated_at) >= now - FOLLOWUP_WINDOW
    if anchor and latest.external_id and (recent_item or marked_visit or POSSIBLE_DROP_BY.search(anchor.body or "") or
                                         estimate_order(db, orders, anchor)):
        if item and utc(item.updated_at) > utc(latest.created_at) and (
                item.thread_message_id or item.last_message_id.startswith("summary:")) and item.status in OPEN_STATUSES:
            return None
        return item, latest, anchor, utc(anchor.created_at)
    if (anchor is None and recent_item and item.last_message_id.startswith("summary:")
            and (item.vehicle_text or item.category in ("repair", "pricing", "scheduling"))):
        if item.status in OPEN_STATUSES or latest and latest.body == MESSAGES["unanswered"]:
            return item, latest, None, utc(item.created_at)
    return None


def _rule(db, orders, anchor, at, now, possible_drop_by):
    order_ids = [order.id for order in orders]
    appointments = db.query(Appointment).filter(Appointment.repair_order_id.in_(order_ids)).all()
    if any(a.status in ("tentative", "confirmed") and utc(a.ends_at) > now for a in appointments):
        return None
    if any(order.status in ("diagnosing", "waiting_parts", "in_progress", "approval_received", "complete") for order in orders):
        return None
    if order_ids and db.query(Payment.id).join(Invoice).filter(
            Invoice.repair_order_id.in_(order_ids), Payment.paid_at >= at.replace(tzinfo=None), Payment.amount > 0,
    ).first():
        return None
    if any(a.status == "completed" and utc(a.ends_at) >= at for a in appointments):
        return None
    visits = [mark for a in appointments if a.status == "cancelled" and utc(a.ends_at) <= now
              and (mark := _marked(db, "followup_missed_visit", a.id)) is not None]
    if visits:
        when = max(utc(visit.created_at) for visit in visits)
        if when >= at:
            return "missed_visit" if now - when >= timedelta(days=1) else None
    if any(a.status == "confirmed" and at <= utc(a.ends_at) <= now for a in appointments):
        return None
    estimate = estimate_order(db, orders, anchor) if anchor else None
    if estimate:
        offsite = offsite_estimate_at(db, estimate.id)
        if (estimate.status == "waiting_approval" and offsite
                and any(line.approval == "pending" for line in estimate.line_items)):
            return "estimate" if now - at >= timedelta(days=2) else None
        return None
    if any(order.status == "waiting_approval" for order in orders):
        return None
    if any(order.status == "delivered" and order.updated_at and utc(order.updated_at) >= at for order in orders):
        return None
    if now - at >= timedelta(days=3):
        return "reactivate"
    if now - at < timedelta(days=1):
        return None
    if possible_drop_by:
        return None
    return "unanswered" if anchor is None or anchor.direction == INBOUND else "waiting"


def candidate(db, customer, now):
    """Resolve one unambiguous customer thread and an exact, repeatable send key."""
    phone = approval_links.mobile(customer.phone)
    if not phone or phone == approval_links.mobile(OWNER_PHONE) or _history_blocks(db, customer.id):
        return None
    orders = db.query(RepairOrder).filter_by(customer_id=customer.id).all()
    activity = _activity(db, customer, orders, now)
    if activity is None:
        return None
    item, latest, anchor, at = activity
    from app.lead_lifecycle import enabled as leads_enabled
    if leads_enabled() and db.query(ServiceLead.id).filter(
            ServiceLead.customer_id == customer.id,
            ServiceLead.status.in_(("declined", "closed_exception")),
            ServiceLead.resolved_at >= at,
    ).first():
        return None
    possible_drop_by = bool(anchor and POSSIBLE_DROP_BY.search(anchor.body or "")
                            or item and POSSIBLE_DROP_BY.search(item.summary or ""))
    rule = _rule(db, orders, anchor, at, now, possible_drop_by)
    if not rule:
        return None
    if latest and latest.body in MESSAGES.values() and (
            rule != "reactivate" or latest.body not in (MESSAGES["waiting"], MESSAGES["unanswered"])
            or now - utc(latest.created_at) < timedelta(days=1)):
        return None
    event = (anchor.external_id or f"sms:{anchor.id}") if anchor else item.last_message_id
    key = f"followup:{rule}:{hashlib.sha256(f'{phone}:{event}'.encode()).hexdigest()[:32]}"
    return {
        "key": key, "itemId": item.id if item else None, "customerId": customer.id, "phone": phone,
        "messageId": latest.external_id if latest else None,
        "callId": None if latest else item.last_message_id.removeprefix("summary:"),
        "text": MESSAGES[rule], "reason": rule, "lastActivity": at,
    }


def _customers(db, now):
    since = now - FOLLOWUP_WINDOW
    return db.query(Customer).filter(or_(
        Customer.id.in_(db.query(Communication.customer_id).filter(Communication.channel == "sms",
                            Communication.created_at >= since.replace(tzinfo=None))),
        Customer.id.in_(db.query(DispatchItem.customer_id).filter(DispatchItem.updated_at >= since)),
    )).order_by(Customer.id).all()


def allowed_now(now):
    from app import alert_rules
    from app.routes.appointments import configured_hours

    local = now.astimezone(PHOENIX)
    hours = configured_hours().get(local.weekday())
    return bool(hours and hours[0] <= local.time() < hours[1] and not alert_rules.in_quiet_hours(local))


def due(db, now):
    """Reserve at most one customer text per five-minute dispatch interval."""
    if not allowed_now(now):
        return []
    if db.bind.dialect.name == "postgresql" and not db.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext('imperial-customer-followups'))")).scalar():
        return []
    if db.query(DispatchOutbound.id).filter(
        DispatchOutbound.kind == KIND, DispatchOutbound.created_at > now - timedelta(minutes=5),
    ).first():
        return []
    for customer in _customers(db, now):
        reminder = candidate(db, customer, now)
        if not reminder or db.query(DispatchOutbound.id).filter_by(dedupe_key=reminder["key"]).first():
            continue
        # The existing unique key also handles a competing worker tick.
        try:
            with db.begin_nested():
                db.add(DispatchOutbound(dedupe_key=reminder["key"], kind=KIND,
                                        item_id=reminder["itemId"], status="reserved", created_at=now))
            db.commit()
            return [reminder]
        except IntegrityError:
            return []
    return []


def board_preview(db, now, limit=30):
    """Show due outreach even when the conversation has no saved task card."""
    rows = []
    for customer in _customers(db, now):
        reminder = candidate(db, customer, now)
        if reminder is None:
            continue
        sent = db.query(DispatchOutbound.status).filter_by(dedupe_key=reminder["key"]).first()
        rows.append({"customer": customer, "when": reminder["lastActivity"].astimezone(PHOENIX),
                     "reason": reminder["reason"], "status": sent[0] if sent else "due"})
    return sorted(rows, key=lambda row: row["when"])[:limit]
