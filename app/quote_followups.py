"""Follow-up draft a few days after a customer declines work on their approval link.

Off by default (setting automatic_declined_followup). Runs from the dispatch tick, makes at most one
draft per five minutes during shop hours, and never sends anything itself: each draft waits on the
dashboard for the owner's Send, with a reminder to tick Offer Snap Finance if it fits that customer.
Every job gets at most one follow-up. Customers who opted out, have a job in progress or waiting on
them, have a visit booked, were texted in the last few days, or have a conversation waiting on the
shop are skipped.

Quotes that are ignored (never answered) are already covered by the existing estimate follow-up in
customer_followups, so this only handles lines the customer actively declined. After a decline the job
leaves waiting_approval, so the two never fire for the same quote.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file, customer_followups
from app.appointment_model import Appointment
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, OPEN_STATUSES
from app.models import Communication, LineItem, RepairOrder

SETTING = "automatic_declined_followup"
AFTER_DAYS = 3
WINDOW_DAYS = 14  # past this the quote is stale; switching on never texts about old declines
QUIET_DAYS = 3
CATEGORY = "declined_followup"
ACTIVE = ("diagnosing", "waiting_parts", "in_progress", "waiting_approval")
LINK_SOURCES = (None, "link")


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def conversation_key(ro_id: int) -> str:
    return f"QUOTE{ro_id}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def declined_lines(ro: RepairOrder) -> list[LineItem]:
    return [li for li in ro.line_items
            if li.approval == "declined" and li.approval_source in LINK_SOURCES and li.decided_at]


def _items_text(lines) -> str:
    """Names the declined work from its labor lines (parts lines repeat them), e.g. "clutch replacement"."""
    labor = [li for li in lines if li.item_type == "labor"] or lines
    names = []
    for li in labor:
        name = re.sub(r"\s*[-:(]?\s*\blabou?r\b\)?\s*", " ", li.description or "", flags=re.I)
        name = " ".join(name.split()) or " ".join((li.description or "").split())
        if name and name.casefold() not in (n.casefold() for n in names):
            names.append(name)
    if not names or len(names) > 3:
        return "work"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def message_for(ro: RepairOrder, lines) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    from app import diag_credit  # the declined-work text carries the diag credit, so the customer gets one text
    credit = f" The ${diag_credit.CREDIT} from your diagnostic still goes toward it." if diag_credit.has_diag(ro) else ""
    return (f"Hi {approval_links.greeting_for(ro)}, I wanted to check back on the {_items_text(lines)} we quoted for "
            f"your {car or 'vehicle'}.{credit} If you'd like to take care of it, or want to "
            f"talk through options, just reply here. Jae")


def _busy(db: Session, ro: RepairOrder, phone: str, now: datetime) -> bool:
    """True when the customer is mid-job, booked, recently texted, or already has a text waiting."""
    if customer_followups._history_blocks(db, ro.customer_id):
        return True
    jobs = db.query(RepairOrder).filter(RepairOrder.customer_id == ro.customer_id).all()
    if any(job.status in ACTIVE for job in jobs):
        return True
    # Part of this quote was approved and not finished yet: the owner is already talking to them.
    if ro.status == "approval_received" and any(li.approval == "approved" for li in ro.line_items):
        return True
    if db.query(Appointment.id).filter(Appointment.repair_order_id.in_([j.id for j in jobs]),
                                       Appointment.starts_at >= now,
                                       Appointment.status.notin_(("cancelled", "canceled"))).first():
        return True
    if db.query(Communication.id).filter(Communication.customer_id == ro.customer_id, Communication.channel == "sms",
                                         Communication.created_at >= (now - timedelta(days=QUIET_DAYS)).replace(tzinfo=None)).first():
        return True
    tail = phone[-10:]
    recent = db.query(DispatchItem).filter(DispatchItem.updated_at >= now - timedelta(days=QUIET_DAYS)).all()
    open_items = db.query(DispatchItem).filter(DispatchItem.status.in_(OPEN_STATUSES)).all()
    return any("".join(c for c in (row.customer_phone or "") if c.isdigit())[-10:] == tail for row in recent + open_items)


def candidates(db: Session, now: datetime):
    """Jobs whose newest link decline is 3 to 17 days old, oldest first."""
    rows = []
    ids = {li.repair_order_id for li in db.query(LineItem).filter(LineItem.approval == "declined").all()}
    for ro in db.query(RepairOrder).filter(RepairOrder.id.in_(ids)).all() if ids else []:
        lines = declined_lines(ro)
        if not lines:
            continue
        at = max(_utc(li.decided_at) for li in lines)
        if timedelta(days=AFTER_DAYS) <= now - at < timedelta(days=AFTER_DAYS + WINDOW_DAYS):
            rows.append((ro, lines, at))
    return sorted(rows, key=lambda row: row[2])


def queue_next(db: Session, now: datetime, fetch=None) -> DispatchItem | None:
    """Makes at most one follow-up draft for the owner to send. Never sends anything itself."""
    if not enabled() or not customer_followups.allowed_now(now):
        return None
    if db.query(DispatchItem.id).filter(DispatchItem.category == CATEGORY,
                                        DispatchItem.created_at > now - timedelta(minutes=5)).first():
        return None
    for ro, lines, _ in candidates(db, now):
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
        total = sum(float(li.total or 0) for li in lines)
        summary = f"Job #{ro.id}: {len(lines)} declined item{'s' if len(lines) != 1 else ''} on the quote"
        summary += f" (${total:,.2f})." if total else "."
        summary += " Tick Offer Snap Finance if it fits."
        item = DispatchItem(quo_conversation_id=conversation_key(ro.id), customer_phone=phone,
                            customer_id=ro.customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                            urgency="normal", summary=summary[:500],
                            proposed_action="Follow up on the declined work", draft_reply=message_for(ro, lines),
                            last_message_id=newest, thread_message_id=newest, status="needs_owner",
                            created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        db.add(DispatchAudit(item_id=item.id, actor="system", action="declined_followup_drafted", detail=f"job {ro.id}"))
        db.commit()
        return item
    return None
