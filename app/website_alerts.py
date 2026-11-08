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


def is_website(item: DispatchItem) -> bool:
    return (item.quo_conversation_id or "").startswith("WEB")
