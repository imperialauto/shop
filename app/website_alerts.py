"""Puts website bookings and requests in the owner's queue, so the normal alert texts cover them.

The item's message id never matches a Quo message, so an approved reply on it is
always skipped by the messaging service. Customers are reached by a person.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app.dispatch_model import OPEN_STATUSES, DispatchAudit, DispatchItem
from app.models import RepairOrder

URGENT_LINES = {"towing", "mobile-roadside"}


def queue(db: Session, order: RepairOrder, kind: str, summary: str, service_line: str = "", vehicle_text: str = "",
          review: bool = False) -> DispatchItem:
    """kind is "booking" or "request". Call inside the caller's transaction, before commit."""
    booking = kind == "booking"
    item = DispatchItem(
        quo_conversation_id=f"WEB{order.id}", customer_phone=order.customer.phone or "", customer_id=order.customer_id,
        vehicle_id=order.vehicle_id, vehicle_text=vehicle_text[:120] or None,
        category="booking" if booking else "estimate",
        urgency="urgent" if service_line in URGENT_LINES else "normal",
        summary=summary[:280], proposed_action=f"Website {'booking' if booking else 'request'} WEB-{order.id}",
        # A confirmed fixed-price booking is FYI. A request, or a booking at a starting price, needs the owner.
        draft_reply="" if booking and not review else None,
        status="routine" if booking and not review else "needs_owner", last_message_id=f"web:{order.id}",
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
    # Its own key, refreshed in place, so a second round of approvals or an open
    # website request for the same job can never collide with it.
    key = f"WEB{order.id}-decision"
    fields = dict(customer_phone=order.customer.phone or "", customer_id=order.customer_id, vehicle_id=order.vehicle_id,
                  category="approval", urgency="normal", summary=summary[:280],
                  proposed_action=f"Customer decision on job #{order.id}", draft_reply=None,
                  last_message_id=f"web:{order.id}:decision:{datetime.now().timestamp():.0f}",
                  updated_at=datetime.now().astimezone())
    item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == key,
                                         DispatchItem.status.in_(OPEN_STATUSES)).first()
    if item is None:
        item = DispatchItem(quo_conversation_id=key, status="needs_owner", **fields)
        db.add(item)
    else:
        for name, value in fields.items():
            setattr(item, name, value)
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor="customer", action="estimate_decided", detail=f"job {order.id}"))
    return item


def queue_reference_help(db: Session, customer, orders: list) -> DispatchItem:
    """A customer lost their reference. One open item per customer, refreshed in place.
    The key starts with WEB, so every no-text guard for website items applies."""
    refs = ", ".join(f"WEB-{o.id}" for o in orders)
    key = f"WEBREF{customer.id}"
    fields = dict(customer_phone=customer.phone or "", customer_id=customer.id, vehicle_id=orders[0].vehicle_id,
                  category="reference_help", urgency="normal",
                  summary=f"Asked for a private vehicle status link. Their open jobs: {refs}.",
                  proposed_action=f"Verify the request, then share the correct private Vehicle Status link for {refs}.", draft_reply=None,
                  last_message_id=f"web:ref:{customer.id}:{datetime.now().timestamp():.0f}",
                  updated_at=datetime.now().astimezone())
    item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == key,
                                         DispatchItem.status.in_(OPEN_STATUSES)).first()
    if item is None:
        item = DispatchItem(quo_conversation_id=key, status="needs_owner", **fields)
        db.add(item)
    else:
        for name, value in fields.items():
            setattr(item, name, value)
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor="website", action="reference_help", detail=refs[:200]))
    return item


def close_for_job(db: Session, order: RepairOrder, actor: str, reason: str) -> int:
    """The job moved forward, so its website booking/request item is done.
    One job, one task: the job page carries it from here. Decision and
    lost-reference items have their own keys and stay open."""
    rows = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == f"WEB{order.id}",
                                         DispatchItem.status.in_(OPEN_STATUSES)).all()
    for item in rows:
        item.status, item.updated_at = "closed", datetime.now().astimezone()
        db.add(DispatchAudit(item_id=item.id, actor=actor, action="website_item_closed_by_job", detail=reason[:200]))
    return len(rows)



def is_website(item: DispatchItem) -> bool:
    return (item.quo_conversation_id or "").startswith("WEB")


ACTIVITY_DEFAULT_HOURS = 24
ACTIVITY_MAX_DAYS = 14


def _utc(value: datetime) -> datetime:
    from datetime import timezone
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def recent(db: Session, since: datetime | None = None, now: datetime | None = None) -> dict:
    """Website bookings, estimate requests and roadside requests that came in since `since`
    (default the last 24 hours, at most 14 days back), newest first. Read only; sends nothing.
    For the owner's app alerts, so a web booking never waits on someone opening the dashboard."""
    from datetime import timedelta, timezone

    from app import launch_offer
    from app.appointment_model import Appointment
    from app.dispatch_model import DispatchOutbound

    now = _utc(now or datetime.now(timezone.utc))
    since = max(_utc(since) if since else now - timedelta(hours=ACTIVITY_DEFAULT_HOURS),
                now - timedelta(days=ACTIVITY_MAX_DAYS))
    # SQLite keeps times without a zone, so filter loosely and re-check in Python.
    rows = (db.query(DispatchItem).filter(DispatchItem.quo_conversation_id.like("WEB%"),
                                          DispatchItem.created_at >= since.replace(tzinfo=None) - timedelta(days=1))
            .order_by(DispatchItem.created_at.desc(), DispatchItem.id.desc()).all())
    out = []
    for item in rows:
        key = item.quo_conversation_id or ""
        if _utc(item.created_at) < since or not key[3:].isdigit():
            continue  # approvals and lost-reference notes have their own keys and tools
        order = db.get(RepairOrder, int(key[3:]))
        booking = item.category == "booking"
        entry = {
            "ref": f"WEB-{key[3:]}", "job_id": int(key[3:]),
            "kind": "booking" if booking else ("roadside request" if item.urgency == "urgent" else "estimate request"),
            "urgent": item.urgency == "urgent", "received_at": _utc(item.created_at).isoformat(),
            "needs_owner": item.status == "needs_owner", "open": item.status in OPEN_STATUSES,
            "customer": " ".join(p for p in ((order.customer.first_name, order.customer.last_name) if order and order.customer else ()) if p),
            "customer_id": item.customer_id, "vehicle": item.vehicle_text or "", "summary": item.summary,
            "launch_offer": bool(order and launch_offer.MARK in (order.concern or "")),
        }
        if booking and order is not None:
            visit = (db.query(Appointment).filter(Appointment.repair_order_id == order.id)
                     .order_by(Appointment.id.desc()).first())
            if visit is not None:
                entry["visit_starts_at"] = _utc(visit.starts_at).isoformat()
                entry["visit_status"] = visit.status
                entry["visit_location"] = visit.location
                sent = (db.query(DispatchOutbound).filter_by(dedupe_key=f"bookconf:{visit.id}").first())
                entry["confirmation_text"] = sent.status if sent else "none"
        out.append(entry)
    return {"since": since.isoformat(), "count": len(out), "items": out}
