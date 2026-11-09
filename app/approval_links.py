"""Approval links by text, through the owner's normal YES flow.

"Send for approval" on a job queues one item for the owner holding the text
and the signing link. Nothing reaches the customer until the owner replies
YES (or approves it on the dashboard), and the messaging service still
refuses to send if the customer has texted since the item was made.
"""
import os
import secrets
from datetime import datetime

from sqlalchemy.orm import Session

from app.dispatch_model import OPEN_STATUSES, DispatchAudit, DispatchItem
from app.models import RepairOrder

CATEGORY = "approval_link"


def conversation_key(ro_id: int) -> str:
    return f"LINK{ro_id}"  # never starts with WEB, so it is not treated as a website item


def base_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://web-production-94989.up.railway.app").rstrip("/")


def mobile(phone) -> str | None:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return "+1" + digits if len(digits) == 10 else None


def message_for(ro: RepairOrder) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else "vehicle"
    first = (ro.customer.first_name or "").strip().split(" ")[0] or "there"
    return (f"Hi {first}, your estimate for the {car} is ready. You can approve or decline each repair here: "
            f"{base_url()}/sign/ro/{ro.public_token} Reply here with any questions. - Imperial Auto Care")


def queue(db: Session, ro: RepairOrder, newest_message_id: str, actor: str) -> DispatchItem:
    """Creates or refreshes the owner's approval item for this job. Sends nothing."""
    phone = mobile(ro.customer.phone)
    if phone is None:
        raise ValueError("no_phone")
    waiting = [li for li in ro.line_items if li.approval == "pending"]
    if not waiting:
        raise ValueError("nothing_pending")
    if not ro.public_token:
        ro.public_token = secrets.token_urlsafe(32)
    total = sum(li.total for li in waiting)
    summary = f"Job #{ro.id}: {len(waiting)} repair{'s' if len(waiting) != 1 else ''}, ${total:,.2f} for the customer to approve."
    now = datetime.now().astimezone()
    item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                         DispatchItem.status.in_(OPEN_STATUSES)).with_for_update().first()
    if item is None:
        item = DispatchItem(quo_conversation_id=conversation_key(ro.id), category=CATEGORY, urgency="normal",
                            status="needs_owner", created_at=now)
        db.add(item)
    item.customer_phone, item.customer_id, item.vehicle_id = phone, ro.customer_id, ro.vehicle_id
    item.summary, item.draft_reply = summary, message_for(ro)
    item.proposed_action = f"Text the approval link for job #{ro.id}"
    # The send is refused later if the customer's thread has moved past this message.
    item.last_message_id = newest_message_id or "none"
    item.thread_message_id = None
    item.updated_at = now
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor=actor[:64], action="approval_link_queued", detail=f"job {ro.id}"))
    return item


def close_for(db: Session, ro: RepairOrder) -> None:
    """The customer answered, so the link item is finished whether or not the owner sent it."""
    rows = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                         DispatchItem.status.in_(OPEN_STATUSES + ("approved",))).all()
    for item in rows:
        item.status, item.updated_at = "closed", datetime.now().astimezone()
        db.add(DispatchAudit(item_id=item.id, actor="customer", action="approval_link_answered", detail=f"job {ro.id}"))
