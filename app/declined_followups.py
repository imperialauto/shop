"""Declined-work reminder: a month or two after a customer passed on work, a short text offering to do it.

Off by default (setting automatic_declined_reminder). Every reminder is a DRAFT in the owner's review
queue (a DispatchItem waiting on the owner, the same queue the approval-link and 3-day declined
follow-up drafts use). Nothing here sends a text or reserves one: a draft goes out only when the owner
presses Send on it, after editing it if he wants.

Which work counts: lines the customer declined (on their link, in person or on the phone) 30 to 60 days
ago. Discounts and the shop's own lines are left out, and so is work the customer had done here since
(a later line on the same vehicle with the same work, not declined). Each declined line is drafted at
most once, ever, so the same work never comes up twice. One draft per customer and vehicle; while a
customer has a reminder waiting on the owner, no second one is made.

The same skips as the 3-day follow-up apply (app/quote_followups.py): opted out, a job in progress or
waiting on them, a visit booked, texted in the last few days, or a conversation already waiting on the
shop. The text names no price, so a customer's "yes" to it never approves work on its own.
"""
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file, customer_followups, quote_followups
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, OPEN_STATUSES
from app.models import LineItem, RepairOrder

SETTING = "automatic_declined_reminder"
MIN_DAYS = 30
MAX_DAYS = 60
CATEGORY = "declined_reminder"
ACTION = "declined_reminder_drafted"
MAX_LINES = 8  # keeps the line list in the audit detail short enough to read back


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def conversation_key(customer_id: int) -> str:
    return f"LATER{customer_id}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _work(description: str) -> str:
    """The work a line names, without labor/parts words: "Front brake pads labor" -> "front brake pads"."""
    text = re.sub(r"\(.*?\)|\b(?:labou?r|parts?|per quart)\b|[-:,]", " ", description or "", flags=re.I)
    return " ".join(text.casefold().split())


def _same_work(a: str, b: str) -> bool:
    a, b = _work(a), _work(b)
    return bool(a and b) and (a in b or b in a)


def drafted_line_ids(db: Session) -> set[int]:
    """Every declined line already drafted, read from the audit trail ("lines 12,13")."""
    ids = set()
    for (detail,) in db.query(DispatchAudit.detail).filter(DispatchAudit.action == ACTION).all():
        ids.update(int(n) for n in re.findall(r"\d+", (detail or "").partition("lines")[2]))
    return ids


def _redone(db: Session, line: LineItem, ro: RepairOrder) -> bool:
    """The customer had this work done here after declining it, or it is already quoted again."""
    decided = _utc(line.decided_at)
    jobs = db.query(RepairOrder).filter(RepairOrder.customer_id == ro.customer_id).all()
    for job in jobs:
        if ro.vehicle_id and job.vehicle_id and job.vehicle_id != ro.vehicle_id:
            continue
        for other in job.line_items:
            if other.id == line.id or other.approval not in (None, "approved", "pending"):
                continue
            when = other.decided_at or other.created_at
            if when is not None and _utc(when) > decided and _same_work(other.description, line.description):
                return True
    return False


def candidates(db: Session, now: datetime) -> list[tuple[RepairOrder, list[LineItem]]]:
    """(newest job, declined lines) per customer and vehicle, oldest decline first."""
    done = drafted_line_ids(db)
    groups: dict[tuple, list] = {}
    rows = db.query(LineItem).filter(LineItem.approval == "declined", LineItem.decided_at.isnot(None)).all()
    for li in rows:
        if li.id in done or li.item_type == "discount" or li.approval_source == "shop" or (li.unit_price or 0) <= 0:
            continue
        if not timedelta(days=MIN_DAYS) <= now - _utc(li.decided_at) <= timedelta(days=MAX_DAYS):
            continue
        ro = li.repair_order
        if ro is None or ro.customer_id is None or _redone(db, li, ro):
            continue
        groups.setdefault((ro.customer_id, ro.vehicle_id), []).append(li)
    result = []
    for lines in groups.values():
        lines.sort(key=lambda li: _utc(li.decided_at))
        lines = lines[:MAX_LINES]
        newest = max(lines, key=lambda li: (_utc(li.decided_at), li.repair_order_id)).repair_order
        result.append((newest, lines))
    return sorted(result, key=lambda row: _utc(row[1][0].decided_at))


def message_for(ro: RepairOrder, lines, now: datetime) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    days = (now - min(_utc(li.decided_at) for li in lines)).days
    when = "Last month" if days < 45 else "A couple of months ago"
    return (f"Hi {approval_links.greeting_for(ro)}, {when.lower()} you passed on the "
            f"{quote_followups._items_text(lines)} for your {car or 'vehicle'}. Want us to take care of it? "
            f"Reply here and we'll set up a time. - Imperial Auto Care")


def queue_next(db: Session, now: datetime, fetch=None) -> DispatchItem | None:
    """Makes at most one reminder draft for the owner's review. Never sends or queues a send."""
    if not enabled() or not customer_followups.allowed_now(now):
        return None
    if db.query(DispatchItem.id).filter(DispatchItem.category == CATEGORY,
                                        DispatchItem.created_at > now - timedelta(minutes=5)).first():
        return None
    for ro, lines in candidates(db, now):
        if db.query(DispatchItem.id).filter(DispatchItem.quo_conversation_id == conversation_key(ro.customer_id),
                                            DispatchItem.status.in_(OPEN_STATUSES)).first():
            continue  # one reminder waiting on the owner per customer
        phone = approval_links.mobile(ro.customer.phone if ro.customer else None)
        if phone is None or phone == approval_links.mobile(OWNER_PHONE) or quote_followups._busy(db, ro, phone, now):
            continue
        thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
        messages = ((thread or {}).get("messages") or []) if problem is None else []
        newest = messages[-1].get("id") if messages else None
        if not newest:
            return None  # the thread can't be checked right now; try again on a later tick
        total = sum(float(li.total or 0) for li in lines)
        jobs = sorted({li.repair_order_id for li in lines})
        summary = (f"Declined about {(now - _utc(lines[0].decided_at)).days} days ago on job "
                   f"{', '.join(f'#{j}' for j in jobs)}: {len(lines)} item{'s' if len(lines) != 1 else ''}"
                   f"{f' (${total:,.2f})' if total else ''}. Draft only, nothing sent. Tick Offer Snap Finance if it fits.")
        item = DispatchItem(quo_conversation_id=conversation_key(ro.customer_id), customer_phone=phone,
                            customer_id=ro.customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                            urgency="normal", summary=summary[:500],
                            proposed_action="Remind them about the work they passed on",
                            draft_reply=message_for(ro, lines, now),
                            last_message_id=newest, thread_message_id=newest, status="needs_owner",
                            created_at=now, updated_at=now)
        db.add(item)
        db.flush()
        db.add(DispatchAudit(item_id=item.id, actor="system", action=ACTION,
                             detail=f"customer {ro.customer_id} lines {','.join(str(li.id) for li in lines)}"[:255]))
        db.commit()
        return item
    return None
