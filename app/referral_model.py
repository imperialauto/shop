"""Referral codes, referrals and store credit. Apply migrations/20261122_referral_credit.sql first."""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String

from app.database import Base


def _now():
    return datetime.now(timezone.utc)


class ReferralCode(Base):
    """One sharing code per customer, like PAT4821."""
    __tablename__ = "referral_codes"
    customer_id = Column(Integer, ForeignKey("customers.id"), primary_key=True)
    code = Column(String(16), nullable=False, unique=True)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class Referral(Base):
    """A new customer who came in on someone's code. A customer can be referred once."""
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    code = Column(String(16), nullable=False)
    referrer_customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    friend_customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, unique=True)
    status = Column(String(12), nullable=False, default="pending")  # pending, earned, void
    friend_ro_id = Column(Integer, ForeignKey("repair_orders.id"))
    discount_line_id = Column(Integer, ForeignKey("line_items.id"))
    created_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    earned_at = Column(DateTime(timezone=True))


class CustomerCredit(Base):
    """Store credit ledger. Earned credit is positive, credit used on a job is negative."""
    __tablename__ = "customer_credits"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    kind = Column(String(16), nullable=False)  # referral, applied, adjust
    referral_id = Column(Integer, ForeignKey("referrals.id"))
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"))
    line_item_id = Column(Integer, ForeignKey("line_items.id"))
    actor = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
