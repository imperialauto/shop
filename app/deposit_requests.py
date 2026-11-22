"""Deposit text after a customer approves on their link.

When a customer approves lines on their approval link, and the owner has switched
"Text the deposit request when a customer approves" on in Settings, one text goes to
the customer asking for a 50% deposit by Zelle or cash, and offering Snap Finance.
The Snap link itself is never sent from here: the customer has to ask for it.

The text goes through the same path as an owner-approved dashboard reply, so the
messaging service only sends it if nothing newer is in the customer's thread, and
never retries it. If the thread can't be checked, the draft waits for the owner's YES
instead. With the setting off (the default), nothing here runs.
"""
import os
from datetime import datetime

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound, OPEN_STATUSES
from app.models import RepairOrder

SETTING = "automatic_deposit_text"
CATEGORY = "deposit_request"
DEPOSIT_SHARE = 0.5  # owner's rule, Nov 2026: half down on every job
DASHBOARD_REPLY = "dashboard_reply"  # the owner-approved reply queue the messaging service sends from


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def zelle_to() -> str:
    return os.getenv("DEPOSIT_ZELLE_TO", "jaelan@imperialautocarellc.com").strip()


def conversation_key(ro_id: int) -> str:
    return f"DEP{ro_id}"  # never starts with WEB, so the no-text website guards don't apply


def amounts(lines) -> tuple[float, float]:
    """(total with tax on parts, 50% deposit), both rounded to cents."""
    from app.routes.invoices import SHOP_TAX_RATE
    subtotal = sum(li.total for li in lines)
    tax = round(sum(li.total for li in lines if li.item_type == "part") * SHOP_TAX_RATE, 2)
    total = round(subtotal + tax, 2)
    return total, round(total * DEPOSIT_SHARE, 2)


def message_for(ro: RepairOrder, lines) -> str:
    total, deposit = amounts(lines)
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    work = f"the work on your {car}" if car else "the work"
    return (f"Hi {approval_links.greeting_for(ro)}, thanks for approving {work}. "
            f"To order parts and get you on the schedule, I'll need a ${deposit:,.2f} deposit, "
            f"half of the ${total:,.2f} total, by Zelle to {zelle_to()} or cash. "
            "We also offer Snap Finance if you'd like to make payments. Just ask and I'll send the link. Jae")


def queue(db: Session, ro: RepairOrder, approved_lines, fetch=None) -> DispatchItem | None:
    """Queues the deposit text for the lines the customer just approved. Returns the item, or None."""
    if not enabled() or not approved_lines:
        return None
    phone = approval_links.mobile(ro.customer.phone if ro.customer else None)
    if phone is None or phone == approval_links.mobile(OWNER_PHONE):
        return None
    total, deposit = amounts(approved_lines)
    if deposit <= 0:
        return None
    if any(p.amount and p.amount > 0 for inv in ro.invoices if inv.status != "void" for p in inv.payments):
        return None  # money is already in on this job; the owner decides what else to ask for
    thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
    messages = (thread or {}).get("messages") or []
    newest = messages[-1].get("id") if messages else None
    now = datetime.now().astimezone()
    # Sent on its own only when the thread could be read; otherwise it waits for the owner's YES.
    automatic = problem is None and bool(newest)
    text = message_for(ro, approved_lines)
    fields = dict(customer_phone=phone, customer_id=ro.customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                  urgency="normal", summary=f"Job #{ro.id}: approved ${total:,.2f}, deposit ${deposit:,.2f} requested.",
                  proposed_action=f"Text the 50% deposit request for job #{ro.id}", draft_reply=text,
                  last_message_id=newest or f"deposit:{ro.id}:{now.timestamp():.0f}",
                  thread_message_id=newest, updated_at=now)
    item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                         DispatchItem.status.in_(OPEN_STATUSES)).with_for_update().first()
    if item is None:
        item = DispatchItem(quo_conversation_id=conversation_key(ro.id), created_at=now,
                            status="approved" if automatic else "needs_owner", **fields)
        db.add(item)
    else:
        for name, value in fields.items():
            setattr(item, name, value)
        item.status = "approved" if automatic else "needs_owner"
    db.flush()
    if automatic:
        item.decided_at = now
        db.add(DispatchOutbound(dedupe_key=f"deposit:{ro.id}:{item.id}:{newest}"[:160], kind=DASHBOARD_REPLY,
                                item_id=item.id, status="queued"))
    db.add(DispatchAudit(item_id=item.id, actor="system", action="deposit_text_queued" if automatic else "deposit_text_held",
                         detail=f"job {ro.id}"))
    return item
