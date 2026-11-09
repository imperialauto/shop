"""How long jobs take, from the customer's first contact to paid (R38 measurement).

Read-only and computed from records that already exist: the customer's first
queue item (text, call or website request), the job, approvals, completion
and the last payment. Used to show whether workflow changes really save time.
"""
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy.orm import Session

from app.dispatch_model import DispatchItem
from app.models import RepairOrder


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def for_job(db: Session, ro: RepairOrder) -> dict:
    """Milestones for one job, oldest first, with hours between them."""
    first = db.query(DispatchItem).filter(DispatchItem.customer_id == ro.customer_id).order_by(DispatchItem.created_at).first()
    contact = _utc(first.created_at) if first else None
    opened = _utc(ro.created_at)
    if contact and opened and contact > opened:
        contact = None  # their first text came after the job was opened; the job is the start
    decided = [_utc(li.decided_at) for li in ro.line_items if li.decided_at]
    payments = [_utc(p.paid_at) for inv in ro.invoices if inv.status != "void" for p in inv.payments if p.paid_at]
    invoiced = [_utc(inv.created_at) for inv in ro.invoices if inv.status != "void" and inv.created_at]
    steps = [("First contact", contact), ("Job opened", opened), ("Customer approved", max(decided) if decided else None),
             ("Invoiced", min(invoiced) if invoiced else None), ("Paid", max(payments) if payments else None)]
    steps = [(label, at) for label, at in steps if at]
    start = steps[0][1] if steps else None
    paid = next((at for label, at in steps if label == "Paid"), None)
    return {"steps": [{"label": label, "at": at, "hours": round((at - start).total_seconds() / 3600, 1)} for label, at in steps],
            "hours_to_paid": round((paid - start).total_seconds() / 3600, 1) if paid and start else None}


def recent(db: Session, days: int = 30, now: datetime | None = None) -> dict:
    """Median hours from first contact to paid for jobs paid in the last N days."""
    now = now or datetime.now(timezone.utc)
    hours = []
    for ro in db.query(RepairOrder).all():
        t = for_job(db, ro)
        paid = next((s["at"] for s in t["steps"] if s["label"] == "Paid"), None)
        if t["hours_to_paid"] is not None and paid and now - paid <= timedelta(days=days):
            hours.append(t["hours_to_paid"])
    return {"jobs": len(hours), "median_hours": round(median(hours), 1) if hours else None, "days": days}
