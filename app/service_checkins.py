"""After-service check-in: one text a week after pickup asking whether the car is running right.

Off by default (setting automatic_service_checkin). When a job is marked picked up, a check-in is
scheduled. Seven days later, inside shop hours, one short text goes out from the shop line through the
owner-approved reply queue, the same way the review text does. It sells nothing and names no price:
work the customer passed on stays with the declined-work reminder (app.declined_followups) and the next
recommended service list (app.recommended_service).

It waits for the owner's YES instead of going out on its own when the thread can't be read, the
customer has texted since pickup, a balance is owed or was waived. It's skipped when the customer
opted out, is back in with another open job, got a check-in in the last 30 days, or the pickup is
more than 14 days old (so switching it on never texts old customers).
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file
from app.appointments import PHOENIX
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound, OPEN_STATUSES
from app.models import Customer, RepairOrder
from app.review_requests import _owed, _texted_since, _waived, car_for

SETTING = "automatic_service_checkin"
KIND = "service_checkin"  # ledger rows only; the messaging service sends dashboard_reply rows
CATEGORY = "service_checkin"
DASHBOARD_REPLY = "dashboard_reply"
AFTER_DAYS = 7
LATEST = timedelta(days=14)
GAP = timedelta(days=30)  # at most one check-in per customer a month
WAITING = "queued"  # queued = waiting; sent = in the send queue; skipped + item_id = held for the owner
CLOSED_STATUSES = ("delivered", "cancelled")


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _prefix(customer_id: int) -> str:
    return f"checkin:{customer_id}:"


def schedule(db: Session, ro: RepairOrder, now: datetime | None = None) -> DispatchOutbound | None:
    """Called when a job is marked picked up."""
    if not enabled() or ro is None or ro.status != "delivered" or not ro.customer_id:
        return None
    key = f"{_prefix(ro.customer_id)}{ro.id}"
    if db.query(DispatchOutbound.id).filter_by(dedupe_key=key).first():
        return None
    now = now or datetime.now(timezone.utc)
    row = DispatchOutbound(dedupe_key=key, kind=KIND, status=WAITING, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    return row


def message_for(ro: RepairOrder) -> str:
    return (f"Hi {approval_links.greeting_for(ro)}, it's Jae at Imperial Auto Care. Checking in on your "
            f"{car_for(ro)} since your visit last week. Is everything running right? "
            f"If anything seems off, just reply here.")


def _recent(db: Session, customer_id: int, now: datetime) -> bool:
    rows = db.query(DispatchOutbound).filter(DispatchOutbound.kind == KIND,
                                             DispatchOutbound.dedupe_key.like(_prefix(customer_id) + "%"),
                                             DispatchOutbound.item_id.isnot(None))
    return any(now - _utc(row.updated_at) < GAP for row in rows)


def _back_in(db: Session, customer_id: int) -> bool:
    return db.query(RepairOrder.id).filter(RepairOrder.customer_id == customer_id,
                                           RepairOrder.status.notin_(CLOSED_STATUSES)).first() is not None


def due(db: Session, now: datetime, fetch=None, allowed=None) -> DispatchItem | None:
    """Turns at most one scheduled check-in into a text or a draft. Called from the dispatch tick."""
    from app import customer_followups

    if not enabled() or not (allowed or customer_followups.allowed_now)(now):
        return None
    today = now.astimezone(PHOENIX).date()
    rows = db.query(DispatchOutbound).filter(DispatchOutbound.kind == KIND, DispatchOutbound.status == WAITING) \
        .order_by(DispatchOutbound.created_at, DispatchOutbound.id).with_for_update().all()
    for row in rows:
        picked_up = _utc(row.created_at)
        if (today - picked_up.astimezone(PHOENIX).date()).days < AFTER_DAYS:
            continue
        if now - picked_up > LATEST:
            row.status, row.updated_at = "skipped", now
            continue
        try:
            customer_id, ro_id = (int(x) for x in row.dedupe_key.removeprefix("checkin:").split(":"))
        except ValueError:
            row.status, row.updated_at = "skipped", now
            continue
        ro = db.get(RepairOrder, ro_id)
        customer = db.get(Customer, customer_id)
        phone = approval_links.mobile(customer.phone if customer else None)
        if (ro is None or ro.customer_id != customer_id or ro.status != "delivered" or phone is None
                or phone == approval_links.mobile(OWNER_PHONE) or _recent(db, customer_id, now)
                or _back_in(db, customer_id) or customer_followups._history_blocks(db, customer_id)):
            row.status, row.updated_at = "skipped", now
            continue
        thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
        messages = (thread or {}).get("messages") or []
        newest = messages[-1].get("id") if messages else None
        automatic = (problem is None and bool(newest) and not _texted_since(db, customer_id, picked_up)
                     and not _owed(ro) and not _waived(ro))
        conversation = f"CHK{ro.id}"
        fields = dict(customer_phone=phone, customer_id=customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                      urgency="normal", summary=f"Job #{ro.id}: picked up a week ago, after-service check-in.",
                      proposed_action=f"Text the after-service check-in for job #{ro.id}",
                      draft_reply=message_for(ro),
                      last_message_id=newest or f"checkin:{ro.id}:{now.timestamp():.0f}",
                      thread_message_id=newest, updated_at=now)
        item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation,
                                             DispatchItem.status.in_(OPEN_STATUSES)).first()
        if item is None:
            item = DispatchItem(quo_conversation_id=conversation, created_at=now,
                                status="approved" if automatic else "needs_owner", **fields)
            db.add(item)
        else:
            for name, value in fields.items():
                setattr(item, name, value)
            item.status = "approved" if automatic else "needs_owner"
        db.flush()
        row.item_id = item.id
        row.status, row.updated_at = ("sent" if automatic else "skipped"), now
        if automatic:
            item.decided_at = now
            db.add(DispatchOutbound(dedupe_key=f"chk:{ro.id}:{item.id}:{newest}"[:160], kind=DASHBOARD_REPLY,
                                    item_id=item.id, status="queued", created_at=now, updated_at=now))
        db.add(DispatchAudit(item_id=item.id, actor="system",
                             action="service_checkin_queued" if automatic else "service_checkin_held",
                             detail=f"job {ro.id}"))
        db.commit()
        return item
    db.commit()
    return None
