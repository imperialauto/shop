"""10% off labor on the next visit.

Every job paid in full earns the customer one next-visit offer: 10% off labor, up to $50,
on a job over $200, used within 90 days of that payment. The offer is printed on the
paid receipt and added as a discount line when the next qualifying job's invoice is made.

One offer per job: a job that already has a discount (like the referral's $25 off) gets no
next-visit discount, and a job that used the offer doesn't earn a new one. The offer is never
tied to leaving a review, because Google doesn't allow incentives for reviews.

Nothing is stored apart from the discount line itself, so this needs no database change.
Off unless the owner turns on "10% off labor on the next visit" in Settings.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import business_settings
from app.models import Invoice, LineItem, RepairOrder
from app.referrals import DISCOUNT, _has_offer, _job_amount, _line

SETTING = "next_visit_offer"
SHARE = 0.10
CAP = 50.0
MIN_JOB = 200.0
GOOD_FOR = timedelta(days=90)
PREFIX = "Next-visit offer: 10% off labor"


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _label(source_ro_id: int) -> str:
    return f"{PREFIX} (from job #{source_ro_id})"


def _used_offer(ro: RepairOrder) -> bool:
    return any(li.item_type == DISCOUNT and (li.description or "").startswith(PREFIX) for li in ro.line_items)


def _paid_on(invoice: Invoice) -> datetime | None:
    dates = [p.paid_at for p in invoice.payments if p.paid_at and (p.amount or 0) > 0]
    return _utc(max(dates)) if dates else None


def earned_by(invoice: Invoice) -> datetime | None:
    """When this paid invoice's offer runs out, or None if it earns none."""
    ro = invoice.repair_order
    if invoice.status != "paid" or ro is None or _used_offer(ro):
        return None
    paid = _paid_on(invoice)
    return paid + GOOD_FOR if paid else None


def open_offer(db: Session, customer_id: int, now: datetime, skip_ro_id: int | None = None):
    """(source job id, expires) for the customer's newest unused offer, or None."""
    rows = db.query(Invoice).join(RepairOrder).filter(
        RepairOrder.customer_id == customer_id, Invoice.status == "paid").all()
    for invoice in sorted(rows, key=lambda i: _paid_on(i) or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
        if invoice.repair_order_id == skip_ro_id:
            continue
        expires = earned_by(invoice)
        if expires is None or expires < now:
            continue
        used = db.query(LineItem.id).filter(LineItem.item_type == DISCOUNT,
                                            LineItem.description == _label(invoice.repair_order_id)).first()
        if used:
            return None  # the newest offer is spent; older ones don't come back
        return invoice.repair_order_id, expires
    return None


def amount_for(ro: RepairOrder) -> float:
    labor = sum(li.total for li in ro.line_items if li.billable and li.item_type == "labor")
    return round(min(labor * SHARE, CAP), 2)


def apply(db: Session, ro: RepairOrder, actor: str = "system", now: datetime | None = None) -> str | None:
    """Called just before a job's invoice is made. Adds the 10% labor discount when the job qualifies."""
    if not enabled() or ro is None or not ro.customer_id or _has_offer(ro) or _job_amount(ro) <= MIN_JOB:
        return None
    offer = open_offer(db, ro.customer_id, now or datetime.now(timezone.utc), skip_ro_id=ro.id)
    off = amount_for(ro)
    if offer is None or off <= 0:
        return None
    _line(db, ro, _label(offer[0]), off, actor)
    return f"{PREFIX} ${off:,.2f}"


def receipt_line(invoice: Invoice) -> str | None:
    """What the paid receipt says about the next visit."""
    if not enabled():
        return None
    expires = earned_by(invoice)
    if expires is None:
        return None
    from app.appointments import PHOENIX
    return (f"Thanks for choosing Imperial Auto Care. Your next visit: 10% off labor, up to $50, on any job over $200. "
            f"Good through {expires.astimezone(PHOENIX).strftime('%B %-d, %Y')}.")
