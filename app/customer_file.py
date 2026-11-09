"""One timeline per customer: texts, calls, jobs, estimates, visits and money.

Everything is matched by the customer record or by the last 10 digits of the
phone number, so items saved before the customer existed still show up.
Read-only: nothing here changes a record or contacts anyone. Texts and calls
come live from the messaging service; when it can't be reached the file says
so instead of looking empty.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.appointment_model import Appointment
from app.dispatch_model import DispatchItem
from app.models import Communication, Customer, Invoice, Payment, RepairOrder

TIMEOUT = 6.0
FILTERS = [("all", "All"), ("text", "Texts"), ("call", "Calls"), ("job", "Jobs"), ("money", "Money")]


def digits(phone) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())[-10:]


def _utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bridge(path: str, phone: str):
    """GET one read-only bridge lookup. Returns (data, problem)."""
    base = os.environ.get("MESSAGING_BRIDGE_URL", "").rstrip("/")
    token = os.environ.get("OPERATIONS_BRIDGE_TOKEN", "")
    if not base or not token:
        return None, "not_connected"
    try:
        r = httpx.get(f"{base}/api/operations{path}", params={"phone": "+1" + phone},
                      headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None, "unavailable"
        return r.json(), None
    except (httpx.HTTPError, ValueError):
        return None, "unavailable"


def _entry(kind, at, title, detail="", link=None, direction=None, media=None):
    return {"kind": kind, "at": _utc(at), "title": title, "detail": detail or "", "link": link,
            "direction": direction, "media": media or []}


def _money(value) -> str:
    return f"${float(value or 0):,.2f}"


def build(db: Session, customer: Customer, fetch=_bridge) -> dict:
    phone = digits(customer.phone)
    entries, problems = [], {}

    # Live texts and calls, fetched together.
    texts = calls = None
    if len(phone) == 10:
        with ThreadPoolExecutor(max_workers=2) as pool:
            t, c = pool.submit(fetch, "/conversation", phone), pool.submit(fetch, "/calls", phone)
            (texts, problems["text"]), (calls, problems["call"]) = t.result(), c.result()
    else:
        problems["text"] = problems["call"] = "no_phone"

    seen = set()
    for m in (texts or {}).get("messages", []):
        seen.add(m.get("id"))
        inbound = m.get("direction") == "INBOUND"
        entries.append(_entry("text", m.get("createdAt"), "Customer texted" if inbound else "Shop texted",
                              m.get("body"), direction="in" if inbound else "out", media=m.get("mediaUrls")))

    # Saved copies, so older texts and notes still show if the live lookup fails or is short.
    comms = db.query(Communication).filter(Communication.customer_id == customer.id).all()
    for c in comms:
        if c.external_id and c.external_id in seen:
            continue
        inbound = c.direction == "inbound"
        kind = "call" if c.channel == "phone" else "text"
        label = {"sms": "texted", "email": "emailed", "phone": "called", "messenger": "messaged"}.get(c.channel, "wrote")
        entries.append(_entry(kind, c.created_at, ("Customer " if inbound else "Shop ") + label, c.body,
                              link=f"/ro/{c.repair_order_id}" if c.repair_order_id else None,
                              direction="in" if inbound else "out"))

    for call in (calls or {}).get("calls", []):
        inbound = call.get("direction") == "INBOUND"
        missed = inbound and not call.get("duration")
        mins = round((call.get("duration") or 0) / 60)
        title = "Missed call" if missed else ("Call from customer" if inbound else "Shop called")
        if call.get("aiHandled"):
            title += " (answered by assistant)"
        if not missed and call.get("duration"):
            title += f" · {mins or '<1'} min"
        detail = call.get("summary") or (f"Voicemail: {call['voicemail']}" if call.get("voicemail") else "")
        entries.append(_entry("call", call.get("createdAt"), title, detail, direction="in" if inbound else "out"))

    # Jobs, estimate decisions, visits and money.
    orders = db.query(RepairOrder).filter(RepairOrder.customer_id == customer.id).all()
    billed = paid = 0.0
    for ro in orders:
        v = ro.vehicle
        car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
        entries.append(_entry("job", ro.created_at, f"Job #{ro.id} opened" + (f" · {car}" if car else ""),
                              f"{ro.concern or ''}  Now: {ro.status.replace('_', ' ')}".strip(), link=f"/ro/{ro.id}"))
        if ro.signed_at:
            entries.append(_entry("job", ro.signed_at, f"Estimate signed for job #{ro.id}",
                                  f"Signed by {ro.approved_by}" if ro.approved_by else "", link=f"/ro/{ro.id}"))
        decided = [li for li in ro.line_items if li.approval in ("approved", "declined") and li.decided_at]
        for li in decided:
            entries.append(_entry("job", li.decided_at, f"Customer {li.approval} a repair on job #{ro.id}",
                                  f"{li.description} · {_money(li.total)}", link=f"/ro/{ro.id}"))
        waiting = [li for li in ro.line_items if li.approval == "pending"]
        if waiting:
            entries.append(_entry("job", max((li.created_at for li in waiting if li.created_at), default=ro.updated_at),
                                  f"Waiting on customer approval for job #{ro.id}",
                                  f"{len(waiting)} item{'s' if len(waiting) != 1 else ''}, "
                                  f"{_money(sum(li.total for li in waiting))}", link=f"/ro/{ro.id}"))
        for inv in ro.invoices:
            if inv.status != "void":
                billed += float(inv.total or 0)
            entries.append(_entry("money", inv.created_at, f"Invoice #{inv.id} · {_money(inv.total)}",
                                  f"Job #{ro.id}, {inv.status}", link=f"/invoices/{inv.id}"))
            for p in inv.payments:
                paid += float(p.amount or 0)
                entries.append(_entry("money", p.paid_at, f"Payment received · {_money(p.amount)}",
                                      f"{(p.method or 'payment').upper()} on invoice #{inv.id}", link=f"/invoices/{inv.id}"))
    if orders:
        for appt in db.query(Appointment).filter(Appointment.repair_order_id.in_([ro.id for ro in orders])).all():
            entries.append(_entry("job", appt.starts_at, f"Visit {appt.status} · {appt.location}",
                                  f"Job #{appt.repair_order_id}", link=f"/ro/{appt.repair_order_id}"))

    # Queue items: by customer record, or by phone when the item came in before the record existed.
    items = db.query(DispatchItem).filter(DispatchItem.customer_id == customer.id).all()
    if len(phone) == 10:
        known = {i.id for i in items}
        items += [i for i in db.query(DispatchItem).filter(DispatchItem.customer_id.is_(None)).all()
                  if i.id not in known and digits(i.customer_phone) == phone]
    for item in items:
        source = "Website request" if (item.quo_conversation_id or "").startswith("WEB") else "Queue item"
        entries.append(_entry("job", item.created_at, f"{source} #{item.id} · {item.category.replace('_', ' ')}",
                              item.summary, link="/#queue"))

    entries = [e for e in entries if e["at"]]
    entries.sort(key=lambda e: e["at"], reverse=True)
    counts = {k: sum(1 for e in entries if k == "all" or e["kind"] == k) for k, _ in FILTERS}
    return {
        "entries": entries,
        "counts": counts,
        "filters": FILTERS,
        "problems": {k: v for k, v in problems.items() if v},
        "money": {"billed": billed, "paid": paid, "balance": round(billed - paid, 2)},
    }
