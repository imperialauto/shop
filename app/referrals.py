"""$25 + $25 referral and store credit.

Every customer can have a sharing code (first name plus four digits, like PAT4821).
A new customer who comes in on a code gets $25 off their first job over $200.
Once that job is paid in full, the customer who shared the code gets $25 store credit.
Store credit comes off their next job automatically when its invoice is made.

Discounts and credit are lines on the job (item type "discount"), so the invoice, its
tax (parts only) and every report see the real price; they are never recorded as payments.
One offer per job: a job that already has a discount line gets no second offer.
Nothing here texts anyone. Off unless REFERRALS_ENABLED is true, which needs the
migration migrations/20261122_referral_credit.sql applied first.
"""
import os
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Customer, Invoice, LineItem, RepairOrder
from app.referral_model import CustomerCredit, Referral, ReferralCode

FRIEND_OFF = 25.0
REFERRER_CREDIT = 25.0
MIN_JOB = 200.0  # the friend's job must be over this, before tax and discounts
DISCOUNT = "discount"
REFERRAL_LINE = "Referral discount: $25 off your first visit"
CREDIT_LINE = "Store credit"
CODE = re.compile(r"^[A-Z]{1,6}\d{4}$")


def enabled() -> bool:
    return os.getenv("REFERRALS_ENABLED", "").strip().lower() == "true"


def _now():
    return datetime.now(timezone.utc)


def clean(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", code or "").upper()


def code_for(db: Session, customer: Customer, actor: str) -> str:
    """The customer's sharing code, made the first time it's asked for."""
    row = db.get(ReferralCode, customer.id)
    if row:
        return row.code
    stem = re.sub(r"[^A-Z]", "", (customer.first_name or "").upper())[:6] or "IAC"
    for _ in range(50):
        code = f"{stem}{secrets.randbelow(10000):04d}"
        if not db.query(ReferralCode.customer_id).filter_by(code=code).first():
            break
    else:
        raise RuntimeError("no free referral code")
    db.add(ReferralCode(customer_id=customer.id, code=code, created_by=actor[:64]))
    db.flush()
    return code


def credit_balance(db: Session, customer_id: int) -> float:
    total = db.query(func.coalesce(func.sum(CustomerCredit.amount), 0.0)).filter(
        CustomerCredit.customer_id == customer_id).scalar()
    return round(float(total or 0), 2)


def _paid_before(db: Session, customer_id: int) -> bool:
    return db.query(Invoice.id).join(RepairOrder).filter(
        RepairOrder.customer_id == customer_id, Invoice.status == "paid").first() is not None


def record(db: Session, friend: Customer, code: str, actor: str) -> tuple[Referral | None, str | None]:
    """Staff types the code a new customer gave. Returns (referral, None) or (None, reason)."""
    code = clean(code)
    owner = db.query(ReferralCode).filter_by(code=code).first() if CODE.match(code) else None
    if owner is None:
        return None, "That referral code wasn't found. Check the letters and four digits."
    if owner.customer_id == friend.id:
        return None, "Customers can't use their own referral code."
    if db.query(Referral.id).filter_by(friend_customer_id=friend.id).first():
        return None, "This customer was already referred."
    if _paid_before(db, friend.id):
        return None, "Referral codes are for new customers. This customer has already paid for a job."
    referral = Referral(code=code, referrer_customer_id=owner.customer_id, friend_customer_id=friend.id,
                        status="pending", created_by=actor[:64])
    db.add(referral)
    db.flush()
    return referral, None


def from_website(db: Session, customer: Customer, code: str | None) -> str:
    """A code typed on the website's booking or estimate form. Records the referral when it's valid
    and returns a note for the job. The website never says whether a code worked."""
    if not enabled() or not clean(code or ""):
        return ""
    given = clean(code)[:16]
    with db.begin_nested():
        referral, problem = record(db, customer, given, "website")
    if referral is None:
        return f"Referral code given: {given}. Not applied: {problem}"
    return f"Referral code given: {given}. $25 off comes off automatically on a first job over $200."


def share_line(db: Session, invoice: Invoice) -> str | None:
    """What a paid receipt says about sharing. Never tied to a review."""
    if not enabled() or invoice is None or invoice.status != "paid" or invoice.repair_order is None:
        return None
    row = db.get(ReferralCode, invoice.repair_order.customer_id)
    if row is None:
        return None
    return (f"Know someone who needs a mechanic? Share your code {row.code}. They get $25 off their first job "
            f"over $200, and you get $25 credit once they pay.")


def _job_amount(ro: RepairOrder) -> float:
    return round(sum(li.total for li in ro.line_items if li.billable and li.item_type != DISCOUNT), 2)


def _has_offer(ro: RepairOrder) -> bool:
    return any(li.item_type == DISCOUNT and li.description != CREDIT_LINE for li in ro.line_items)


def _line(db: Session, ro: RepairOrder, description: str, amount: float, actor: str) -> LineItem:
    now = _now()
    line = LineItem(repair_order_id=ro.id, description=description, item_type=DISCOUNT, quantity=1.0,
                    unit_price=-round(amount, 2), taxable=False, approval="approved", decided_at=now,
                    approval_source="shop", approval_recorded_by=actor[:64], completed_at=now)
    db.add(line)
    db.flush()
    if line not in ro.line_items:
        ro.line_items.append(line)
    return line


def apply_to_job(db: Session, ro: RepairOrder, actor: str = "system") -> list[str]:
    """Called just before a job's invoice is made. Adds the friend's discount and any store credit."""
    if not enabled() or ro is None or not ro.customer_id:
        return []
    added = []
    referral = db.query(Referral).filter_by(friend_customer_id=ro.customer_id, status="pending").first()
    if (referral and referral.discount_line_id is None and not _has_offer(ro)
            and _job_amount(ro) > MIN_JOB):
        line = _line(db, ro, REFERRAL_LINE, FRIEND_OFF, actor)
        referral.friend_ro_id, referral.discount_line_id = ro.id, line.id
        added.append(REFERRAL_LINE)
    if not any(li.description == CREDIT_LINE and li.item_type == DISCOUNT for li in ro.line_items):
        balance = credit_balance(db, ro.customer_id)
        owed = round(sum(li.total for li in ro.line_items if li.billable), 2)
        use = round(min(balance, owed), 2)
        if use > 0:
            line = _line(db, ro, CREDIT_LINE, use, actor)
            db.add(CustomerCredit(customer_id=ro.customer_id, amount=-use, kind="applied",
                                  repair_order_id=ro.id, line_item_id=line.id, actor=actor[:64]))
            added.append(f"{CREDIT_LINE} ${use:,.2f}")
    return added


def on_paid(db: Session, invoice: Invoice) -> CustomerCredit | None:
    """When a job is paid in full, its customer gets a sharing code for the receipt. When it's the friend's
    discounted job, the referrer also earns their credit, once."""
    if not enabled() or invoice is None or invoice.status != "paid" or invoice.paid_total <= 0:
        return None
    ro = invoice.repair_order
    if ro is not None and ro.customer is not None:
        code_for(db, ro.customer, "system")
    referral = db.query(Referral).filter_by(friend_ro_id=invoice.repair_order_id, status="pending").first()
    if referral is None or referral.discount_line_id is None:
        return None
    if db.query(CustomerCredit.id).filter_by(referral_id=referral.id, kind="referral").first():
        return None
    referral.status, referral.earned_at = "earned", _now()
    credit = CustomerCredit(customer_id=referral.referrer_customer_id, amount=REFERRER_CREDIT, kind="referral",
                            referral_id=referral.id, repair_order_id=invoice.repair_order_id, actor="system")
    db.add(credit)
    db.flush()
    return credit


def summary(db: Session, customer: Customer) -> dict:
    """What the customer page shows."""
    code = db.get(ReferralCode, customer.id)
    referred_by = db.query(Referral).filter_by(friend_customer_id=customer.id).first()
    sent = db.query(Referral).filter_by(referrer_customer_id=customer.id).all()
    return {"code": code.code if code else None, "credit": credit_balance(db, customer.id),
            "referred_by": referred_by,
            "referrer": db.get(Customer, referred_by.referrer_customer_id) if referred_by else None,
            "can_be_referred": referred_by is None and not _paid_before(db, customer.id),
            "sent": len(sent), "earned": sum(1 for r in sent if r.status == "earned")}


def report(db: Session) -> dict:
    """Usage for the owner: how many referrals came in and what they cost."""
    rows = db.query(Referral).all()
    earned = [r for r in rows if r.status == "earned"]
    given = db.query(func.coalesce(func.sum(CustomerCredit.amount), 0.0)).filter(CustomerCredit.kind == "referral").scalar()
    used = db.query(func.coalesce(func.sum(CustomerCredit.amount), 0.0)).filter(CustomerCredit.kind == "applied").scalar()
    return {"referrals": len(rows), "earned": len(earned), "discount_given": round(FRIEND_OFF * sum(1 for r in rows if r.discount_line_id), 2),
            "credit_given": round(float(given or 0), 2), "credit_used": round(-float(used or 0), 2)}
