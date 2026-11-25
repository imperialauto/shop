"""Ready-for-pickup text when the owner marks a job Complete.

Off by default (setting automatic_pickup_text). Sends at most one pickup text per job, on the same
owner-approved reply queue as the deposit text, so the messaging service only sends it when nothing
newer is in the customer's thread and never retries it. With no invoice to read a balance from, or a
thread it can't check, the draft waits for the owner's YES instead.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file, deposit_requests, payment_options, security_controls
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound
from app.models import RepairOrder

SETTING = "automatic_pickup_text"
CATEGORY = "pickup_text"
DASHBOARD_REPLY = "dashboard_reply"
STORAGE_LINE = "Storage of $25 per day starts 3 days after this notice."


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def conversation_key(ro_id: int) -> str:
    return f"PICKUP{ro_id}"


def balance_due(ro: RepairOrder) -> float | None:
    """What's left on the job's invoice, or None when there is no invoice to read it from."""
    invoices = [inv for inv in ro.invoices if inv.status != "void"]
    if not invoices:
        return None
    return round(sum(inv.balance for inv in invoices), 2)


def message_for(ro: RepairOrder, balance: float | None) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    text = f"Hi {approval_links.greeting_for(ro)}, your {car or 'vehicle'} is done and ready for pickup. "
    if balance is not None and balance <= 0:
        text += "You're all paid up. "
    elif balance is not None:
        text += f"The balance due is ${balance:,.2f}, by {payment_options.phrase()}. "
    # The storage fee is only owed where the customer agreed to the terms when they signed.
    if security_controls.value("terms_on_approval_page"):
        text += STORAGE_LINE + " "
    return text + "Jae"


def queue(db: Session, ro: RepairOrder, fetch=None) -> DispatchItem | None:
    """Queues the pickup text once per job. Returns the item, or None when nothing should go."""
    if not enabled():
        return None
    phone = approval_links.mobile(ro.customer.phone if ro.customer else None)
    if phone is None or phone == approval_links.mobile(OWNER_PHONE):
        return None
    key = conversation_key(ro.id)
    if db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == key).first():
        return None  # a pickup text was already queued for this job; moving it back and forth sends nothing new
    balance = balance_due(ro)
    thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
    messages = ((thread or {}).get("messages") or []) if problem is None else []
    newest = messages[-1].get("id") if messages else None
    automatic = problem is None and bool(newest) and balance is not None
    now = datetime.now().astimezone()
    if balance is None:
        summary = f"Job #{ro.id} is complete. No invoice yet, so the text has no balance. Check it before sending."
    else:
        summary = f"Job #{ro.id} is complete. Pickup text with ${balance:,.2f} due."
    item = DispatchItem(quo_conversation_id=key, customer_phone=phone, customer_id=ro.customer_id,
                        vehicle_id=ro.vehicle_id, category=CATEGORY, urgency="normal", summary=summary,
                        proposed_action=f"Text the pickup notice for job #{ro.id}",
                        draft_reply=message_for(ro, balance),
                        last_message_id=newest or f"pickup:{ro.id}:{now.timestamp():.0f}",
                        thread_message_id=newest, status="approved" if automatic else "needs_owner",
                        created_at=now, updated_at=now)
    db.add(item)
    db.flush()
    if automatic:
        db.add(DispatchOutbound(dedupe_key=f"pickup:{ro.id}:{item.id}:{newest}"[:160], kind=DASHBOARD_REPLY,
                                item_id=item.id, status="queued"))
    db.add(DispatchAudit(item_id=item.id, actor="system",
                         action="pickup_text_queued" if automatic else "pickup_text_held", detail=f"job {ro.id}"))
    return item
