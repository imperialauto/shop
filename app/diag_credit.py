"""Reminder draft after a diagnostic-only visit: the $75 diag credit still goes toward the repair.

Off by default (setting automatic_diag_credit_reminder). Runs from the dispatch tick, makes at most one
draft per five minutes during shop hours, and never sends anything itself: each draft waits on the
dashboard for the owner's Send, with a reminder to tick Offer Snap Finance if it fits.

A job counts when it is finished, its only labor was the diagnostic, and it was paid or closed 2 to 16
days ago. Jobs where the customer declined work on their link are left to the declined-work follow-up,
which mentions the credit itself, so a customer never gets both. Customers who came back since, opted
out, have a job open or booked, were texted in the last few days, or have a conversation waiting on
the shop are skipped. Every job gets at most one reminder.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file, customer_followups, quote_followups
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem
from app.models import RepairOrder

SETTING = "automatic_diag_credit_reminder"
CREDIT = 75
AFTER_DAYS = 2
WINDOW_DAYS = 14
CATEGORY = "diag_credit"
DONE = ("complete", "delivered")
DIAG = re.compile(r"\bdiag(?:nostic|nostics|nosis|s)?\b", re.I)


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def conversation_key(ro_id: int) -> str:
    return f"DIAG{ro_id}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def has_diag(ro: RepairOrder) -> bool:
    return any(DIAG.search(li.description or "") and li.billable and li.approval in (None, "approved")
               for li in ro.line_items)


def diag_only(ro: RepairOrder) -> bool:
    labor = [li for li in ro.line_items
             if li.item_type == "labor" and li.billable and li.approval in (None, "approved")]
    return bool(labor) and all(DIAG.search(li.description or "") for li in labor)


def done_at(ro: RepairOrder) -> datetime:
    paid = [p.paid_at for inv in ro.invoices if inv.status != "void" for p in inv.payments if p.paid_at]
    return _utc(max(paid) if paid else (ro.updated_at or ro.created_at))


def message_for(ro: RepairOrder) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    return (f"Hi {approval_links.greeting_for(ro)}, just a reminder that the ${CREDIT} from the diagnostic on your "
            f"{car or 'vehicle'} still goes toward the repair if you'd like us to take care of it. "
            f"Just reply here and I'll get you on the schedule. Jae")


def candidates(db: Session, now: datetime):
    rows = []
    for ro in db.query(RepairOrder).filter(RepairOrder.status.in_(DONE)).all():
        if not diag_only(ro) or quote_followups.declined_lines(ro):
            continue
        at = done_at(ro)
        if timedelta(days=AFTER_DAYS) <= now - at < timedelta(days=AFTER_DAYS + WINDOW_DAYS):
            rows.append((ro, at))
    return sorted(rows, key=lambda row: row[1])


def came_back(db: Session, ro: RepairOrder) -> bool:
    return db.query(RepairOrder.id).filter(RepairOrder.customer_id == ro.customer_id, RepairOrder.id > ro.id).first() is not None


def queue_next(db: Session, now: datetime, fetch=None) -> DispatchItem | None:
    """Makes at most one diag credit draft for the owner to send. Never sends anything itself."""
    if not enabled() or not customer_followups.allowed_now(now):
        return None
    if db.query(DispatchItem.id).filter(DispatchItem.category == CATEGORY,
                                        DispatchItem.created_at > now - timedelta(minutes=5)).first():
        return None
    for ro, _ in candidates(db, now):
        if db.query(DispatchItem.id).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id)).first():
            continue
        phone = approval_links.mobile(ro.customer.phone if ro.customer else None)
        if (phone is None or phone == approval_links.mobile(OWNER_PHONE) or came_back(db, ro)
                or quote_followups._busy(db, ro, phone, now)):
            continue
        thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
        messages = ((thread or {}).get("messages") or []) if problem is None else []
        newest = messages[-1].get("id") if messages else None
        if not newest:
            return None  # the thread can't be checked right now; try again on a later tick
        item = DispatchItem(quo_conversation_id=conversation_key(ro.id), customer_phone=phone,
                            customer_id=ro.customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                            urgency="normal",
                            summary=f"Job #{ro.id}: diagnostic done, no repair yet. Tick Offer Snap Finance if it fits.",
                            proposed_action=f"Remind them the ${CREDIT} diag credit is waiting",
                            draft_reply=message_for(ro), last_message_id=newest, thread_message_id=newest,
                            status="needs_owner", created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        db.add(DispatchAudit(item_id=item.id, actor="system", action="diag_credit_drafted", detail=f"job {ro.id}"))
        db.commit()
        return item
    return None
