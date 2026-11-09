"""Puts website bookings and requests in the owner's queue, so the normal alert texts cover them.

The item's message id never matches a Quo message, so an approved reply on it is
always skipped by the messaging service. Customers are reached by a person.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.dispatch_model import DispatchAudit, DispatchItem
from app.models import RepairOrder

URGENT_LINES = {"towing", "mobile-roadside"}


def queue(db: Session, order: RepairOrder, kind: str, summary: str, service_line: str = "", vehicle_text: str = "") -> DispatchItem:
    """kind is "booking" or "request". Call inside the caller's transaction, before commit."""
    booking = kind == "booking"
    item = DispatchItem(
        quo_conversation_id=f"WEB{order.id}", customer_phone=order.customer.phone or "", customer_id=order.customer_id,
        vehicle_id=order.vehicle_id, vehicle_text=vehicle_text[:120] or None,
        category="booking" if booking else "estimate",
        urgency="urgent" if service_line in URGENT_LINES else "normal",
        summary=summary[:280], proposed_action=f"Website {'booking' if booking else 'request'} WEB-{order.id}",
        draft_reply="" if booking else None,  # a confirmed booking is FYI, a request needs the owner
        status="routine" if booking else "needs_owner", last_message_id=f"web:{order.id}",
        updated_at=datetime.now().astimezone())
    db.add(item)
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor="website", action="website_" + kind, detail=f"WEB-{order.id}"))
    return item


def queue_decision(db: Session, order: RepairOrder, decided: list) -> DispatchItem:
    """Tells the owner what the customer approved. Never texts the customer."""
    yes = [li for li in decided if li.approval == "approved"]
    no = [li for li in decided if li.approval == "declined"]
    amount = sum(li.total for li in yes)
    summary = f"Approved {len(yes)} of {len(decided)} items, ${amount:,.2f}."
    if no:
        summary += " Declined: " + "; ".join(li.description[:40] for li in no)
    item = DispatchItem(
        quo_conversation_id=f"WEB{order.id}", customer_phone=order.customer.phone or "", customer_id=order.customer_id,
        vehicle_id=order.vehicle_id, category="approval", urgency="normal", summary=summary[:280],
        proposed_action=f"Customer decision on job #{order.id}", draft_reply=None, status="needs_owner",
        last_message_id=f"web:{order.id}:decision", updated_at=datetime.now().astimezone())
    db.add(item)
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor="customer", action="estimate_decided", detail=f"job {order.id}"))
    return item


def is_website(item: DispatchItem) -> bool:
    return (item.quo_conversation_id or "").startswith("WEB")
