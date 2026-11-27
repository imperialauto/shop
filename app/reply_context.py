"""Everything a reply draft may say, gathered in one place.

build(db, customer_id) reads the customer's stored texts (Communication rows), the
customer, their vehicle and their open job: quote lines and totals, approvals, tech
notes, the next appointment, parts orders and payments. It returns a plain dict, so
the rule-based drafter (app/reply_drafts.py) and any AI drafter read the same facts,
and so a thread can be saved as JSON and replayed outside the database
(scripts/draft_preview.py).

It only reads. Facts the shop does not have are left out (None), never guessed: the
drafter turns a missing fact into an honest "let me check" and a needs-info flag.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.appointments import PHOENIX

HISTORY_LIMIT = 40
STYLE_SAMPLE = 25
GREETINGS = ("hi", "hey", "hello", "good morning", "good afternoon", "morning")
PLACEHOLDER_NAMES = {"unknown", "customer", "guest", "lead", "vehicle", "new", "test", "quo"}
# Texts the system writes itself; they say nothing about how Jae writes.
_AUTOMATED = re.compile(r"(- Imperial Auto Care\s*$|/status/|Tap Review|Reply STOP)", re.I)


def _iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def first_name(customer, contact_name: str | None = None) -> str | None:
    for raw in ((getattr(customer, "first_name", None) or ""), (contact_name or "")):
        parts = raw.strip().split()
        word = parts[0].strip(".,:-") if parts else ""
        if word.isalpha() and word.casefold() not in PLACEHOLDER_NAMES:
            return word[:1].upper() + word[1:]
    return None


def style_from(outbound_bodies: list[str]) -> dict:
    """Simple cues from Jae's own past texts: how he opens, whether he uses names and '!'."""
    own = [b.strip() for b in outbound_bodies if b and b.strip() and not _AUTOMATED.search(b)][:STYLE_SAMPLE]
    style = {"greeting": "Hi", "uses_name": True, "exclaims": False, "avg_words": 0, "sample_size": len(own)}
    if not own:
        return style
    openers = {}
    named = 0
    for body in own:
        lowered = body.lower()
        for greet in GREETINGS:
            if lowered.startswith(greet):
                openers[greet] = openers.get(greet, 0) + 1
                rest = body[len(greet):].lstrip(" ,")
                if rest[:1].isupper() and re.match(r"^[A-Z][a-z]+[,! ]", rest):
                    named += 1
                break
    greeted = sum(openers.values())
    if openers and greeted * 2 >= len(own):
        top = max(openers, key=openers.get)
        style["greeting"] = top[:1].upper() + top[1:]
    elif greeted * 4 < len(own):
        style["greeting"] = None  # he usually gets straight to the point
    style["uses_name"] = greeted == 0 or named * 2 >= greeted
    style["exclaims"] = sum("!" in b for b in own) * 3 > len(own)
    style["avg_words"] = round(sum(len(b.split()) for b in own) / len(own))
    return style


def _line(li) -> dict:
    return {"description": (li.description or "").strip()[:120], "type": li.item_type or "labor",
            "qty": li.quantity or 0, "unit_price": li.unit_price or 0, "total": round((li.quantity or 0) * (li.unit_price or 0), 2),
            "approval": li.approval}


def _job(db: Session, ro, now: datetime) -> dict:
    from app import job_deposits
    from app.appointment_model import Appointment
    from app.models import PartsOrder
    from app.public_links import base_url

    lines = [_line(li) for li in ro.line_items]
    approvals = {"pending": 0, "approved": 0, "declined": 0, "not_sent": 0}
    for li in ro.line_items:
        approvals[li.approval if li.approval in approvals else "not_sent"] += 1
    priced = [li for li in ro.line_items if li.approval != "declined"]
    total = job_deposits.job_total(ro) if priced else None
    deposits = job_deposits.total(ro)
    invoice = job_deposits.open_invoice(ro)
    invoice_info = None
    if invoice is not None:
        paid = round(sum(p.amount or 0 for p in invoice.payments), 2)
        due = round((invoice.total or 0) - (invoice.waived_amount or 0) - paid, 2)
        invoice_info = {"status": invoice.status, "total": round(invoice.total or 0, 2), "paid": paid,
                        "balance": max(due, 0.0)}
    parts = db.query(PartsOrder).filter(PartsOrder.repair_order_id == ro.id).order_by(PartsOrder.ordered_at).all()
    appointment = (db.query(Appointment)
                   .filter(Appointment.repair_order_id == ro.id, Appointment.starts_at >= now,
                           Appointment.status.in_(("tentative", "confirmed")))
                   .order_by(Appointment.starts_at).first())
    vehicle = ro.vehicle
    return {
        "id": ro.id,
        "status": ro.status or "intake",
        "concern": (ro.concern or "").strip()[:300] or None,
        "tech_notes": (ro.tech_notes or "").strip()[:600] or None,
        "vehicle": " ".join(str(x) for x in (vehicle.year, vehicle.make, vehicle.model) if x) if vehicle else None,
        "lines": lines,
        "approvals": approvals,
        "total": total,  # tax included on parts, declined lines left out
        "deposits_paid": deposits,
        "invoice": invoice_info,
        "promised_at": _iso(ro.promised_date),
        "status_link": f"{base_url()}/status/{ro.public_token}" if ro.public_token else None,
        "appointment": {"starts_at": _iso(appointment.starts_at), "status": appointment.status,
                        "location": appointment.location} if appointment else None,
        "parts": [{"description": (p.description or p.part_number or "part").strip()[:80],
                   "status": p.status or "ordered", "ordered_at": _iso(p.ordered_at),
                   "received_at": _iso(p.received_at), "eta": None}  # the shop records no ETA on orders
                  for p in parts if (p.status or "") != "returned"],
    }


def build(db: Session, customer_id: int | None, item=None, now: datetime | None = None,
          limit: int = HISTORY_LIMIT) -> dict:
    """The facts for one conversation. `item` (a DispatchItem) supplies the newest text
    when the stored history hasn't caught up yet."""
    from app.models import Communication, Customer, RepairOrder, Vehicle
    from app.ro_status_policy import CLOSED_STATUSES

    now = now or datetime.now(timezone.utc)
    customer = db.get(Customer, customer_id) if customer_id else None
    rows = []
    if customer is not None:
        rows = (db.query(Communication).filter(Communication.customer_id == customer.id)
                .order_by(Communication.created_at.desc(), Communication.id.desc()).limit(limit).all())
    conversation = [{"direction": (r.direction or "outbound").lower(), "body": (r.body or "").strip(),
                     "at": _iso(r.created_at), "channel": r.channel or "sms"} for r in reversed(rows)]
    latest = item.summary.strip() if item is not None and (item.summary or "").strip() else None
    if latest and not any(m["direction"] == "inbound" and m["body"][:len(latest)] == latest for m in conversation[-3:]):
        conversation.append({"direction": "inbound", "body": latest, "at": _iso(getattr(item, "updated_at", None)),
                             "channel": "sms"})
    vehicle = None
    if customer is not None:
        chosen = db.get(Vehicle, item.vehicle_id) if item is not None and item.vehicle_id else None
        chosen = chosen or db.query(Vehicle).filter(Vehicle.customer_id == customer.id).order_by(Vehicle.id.desc()).first()
        if chosen is not None:
            vehicle = {"label": " ".join(str(x) for x in (chosen.year, chosen.make, chosen.model) if x) or None,
                       "has_vin": bool((chosen.vin or "").strip())}
    if vehicle is None and item is not None and item.vehicle_text:
        vehicle = {"label": item.vehicle_text, "has_vin": False}
    job = None
    if customer is not None:
        ro = (db.query(RepairOrder)
              .filter(RepairOrder.customer_id == customer.id,
                      or_(RepairOrder.status.is_(None), ~RepairOrder.status.in_(CLOSED_STATUSES)))
              .order_by(RepairOrder.created_at.desc(), RepairOrder.id.desc()).first())
        if ro is not None:
            job = _job(db, ro, now)
    return {
        "customer": {"id": customer.id if customer else None,
                     "first_name": first_name(customer, getattr(item, "contact_name", None))},
        "vehicle": vehicle,
        "conversation": conversation,
        "job": job,
        "style": style_from([m["body"] for m in reversed(conversation) if m["direction"] == "outbound"]),
        "now": now.isoformat(),
    }


def latest_inbound(context: dict) -> str:
    for message in reversed(context.get("conversation") or []):
        if message.get("direction") == "inbound" and (message.get("body") or "").strip():
            return message["body"].strip()
    return ""


def shop_time(iso_value: str | None) -> datetime | None:
    if not iso_value:
        return None
    try:
        value = datetime.fromisoformat(iso_value)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(PHOENIX)
