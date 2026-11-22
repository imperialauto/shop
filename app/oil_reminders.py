"""Oil change due reminder, a few months after the customer's last oil change here.

Off by default (setting automatic_oil_reminders). Runs from the dispatch tick only when nothing else is
going out, queues at most one reminder per five minutes, during shop hours, on the same owner-approved
reply queue as the deposit and pickup texts. Every job gets at most one reminder. Customers who opted
out, have a job open or booked, were texted in the last week, or have a conversation waiting on the
shop are skipped, so it never lands on top of another text.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file, customer_followups
from app.appointment_model import Appointment
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound, OPEN_STATUSES
from app.models import Communication, RepairOrder

SETTING = "automatic_oil_reminders"
MONTHS_SETTING = "oil_reminder_months"
DEFAULT_MONTHS = 5
WINDOW_DAYS = 60  # past this, the visit is too old to remind about; switching on never texts years-old customers
QUIET_DAYS = 7
CATEGORY = "oil_reminder"
DASHBOARD_REPLY = "dashboard_reply"
DONE = ("complete", "delivered")
OIL = re.compile(r"\b(?:oil\s*(?:&|and)?\s*filter|oil\s+change|lube,?\s*oil|LOF)\b", re.I)


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def months() -> int:
    try:
        value = int((business_settings.raw(MONTHS_SETTING) or "").strip())
    except ValueError:
        return DEFAULT_MONTHS
    return value if 1 <= value <= 24 else DEFAULT_MONTHS


def conversation_key(ro_id: int) -> str:
    return f"OIL{ro_id}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_oil_change(ro: RepairOrder) -> bool:
    return any(OIL.search(li.description or "") and li.billable and li.approval in (None, "approved")
               for li in ro.line_items)


def done_at(ro: RepairOrder) -> datetime:
    """When the oil change was done: the last payment on the job, else the job's last change."""
    paid = [p.paid_at for inv in ro.invoices if inv.status != "void" for p in inv.payments if p.paid_at]
    return _utc(max(paid) if paid else (ro.updated_at or ro.created_at))


def message_for(ro: RepairOrder) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    return (f"Hi {approval_links.greeting_for(ro)}, it's been about {months()} months since the last oil change "
            f"on your {car or 'vehicle'}, so it's probably coming due. Want me to get you on the schedule? Jae")


def _busy(db: Session, ro: RepairOrder, phone: str, now: datetime) -> bool:
    """True when another text, an open job or a booking means this reminder would pile on."""
    customer_id = ro.customer_id
    if customer_followups._history_blocks(db, customer_id):
        return True
    jobs = db.query(RepairOrder).filter(RepairOrder.customer_id == customer_id).all()
    if any(job.status not in DONE for job in jobs):
        return True
    if db.query(Appointment.id).filter(Appointment.repair_order_id.in_([j.id for j in jobs]),
                                       Appointment.starts_at >= now,
                                       Appointment.status.notin_(("cancelled", "canceled"))).first():
        return True
    if db.query(Communication.id).filter(Communication.customer_id == customer_id, Communication.channel == "sms",
                                         Communication.created_at >= (now - timedelta(days=QUIET_DAYS)).replace(tzinfo=None)).first():
        return True
    tail = phone[-10:]
    recent = db.query(DispatchItem).filter(DispatchItem.updated_at >= now - timedelta(days=QUIET_DAYS)).all()
    open_items = db.query(DispatchItem).filter(DispatchItem.status.in_(OPEN_STATUSES)).all()
    return any("".join(c for c in (row.customer_phone or "") if c.isdigit())[-10:] == tail for row in recent + open_items)


def candidates(db: Session, now: datetime):
    """Each customer's most recent finished oil change job that is due now, oldest first."""
    latest = {}
    for ro in db.query(RepairOrder).filter(RepairOrder.status.in_(DONE)).all():
        if is_oil_change(ro):
            at = done_at(ro)
            if ro.customer_id not in latest or at > latest[ro.customer_id][1]:
                latest[ro.customer_id] = (ro, at)
    due_after = timedelta(days=round(months() * 30.4))
    rows = [(ro, at) for ro, at in latest.values() if due_after <= now - at < due_after + timedelta(days=WINDOW_DAYS)]
    return sorted(rows, key=lambda row: row[1])


def queue_next(db: Session, now: datetime, fetch=None) -> DispatchItem | None:
    """Queues at most one oil reminder. The messaging service sends it on its next tick."""
    if not enabled() or not customer_followups.allowed_now(now):
        return None
    if db.query(DispatchItem.id).filter(DispatchItem.category == CATEGORY,
                                        DispatchItem.created_at > now - timedelta(minutes=5)).first():
        return None
    for ro, _ in candidates(db, now):
        if db.query(DispatchItem.id).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id)).first():
            continue
        phone = approval_links.mobile(ro.customer.phone if ro.customer else None)
        if phone is None or phone == approval_links.mobile(OWNER_PHONE) or _busy(db, ro, phone, now):
            continue
        thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
        messages = ((thread or {}).get("messages") or []) if problem is None else []
        newest = messages[-1].get("id") if messages else None
        if not newest:
            return None  # the thread can't be checked right now; try again on a later tick
        item = DispatchItem(quo_conversation_id=conversation_key(ro.id), customer_phone=phone,
                            customer_id=ro.customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                            urgency="normal", summary=f"Oil change due: last one on job #{ro.id}.",
                            proposed_action="Text the oil change reminder", draft_reply=message_for(ro),
                            last_message_id=newest, thread_message_id=newest, status="approved",
                            created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        db.add(DispatchOutbound(dedupe_key=f"oil:{ro.id}:{item.id}:{newest}"[:160], kind=DASHBOARD_REPLY,
                                item_id=item.id, status="queued", created_at=now, updated_at=now))
        db.add(DispatchAudit(item_id=item.id, actor="system", action="oil_reminder_queued", detail=f"job {ro.id}"))
        db.commit()
        return item
    return None
