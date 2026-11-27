"""Owner money tracker: account balances, bills and money expected in.

Figures are typed or pasted in by the owner on /finance/. Nothing here imports bank
data or touches invoices and payments. Amounts are whole cents.
Production migration: migrations/20261122_finance_tracker.sql (not auto-applied).
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, Date, DateTime, Index, Integer, String, Text

from app.database import Base

SCOPES = ("business", "personal")
BILL_CATEGORIES = ("rent", "utilities", "loan", "card", "software", "phone", "insurance", "vehicle", "parts", "taxes", "fees", "other")
REPEATS = ("once", "weekly", "monthly")
# Bill notes that mean something to the page. Kept in the note column so no table changes are needed.
NOTE_UNCONFIRMED = "Not confirmed yet"
NOTE_PAID_GUESS = "Paid date not confirmed"
MARKER_NOTES = (NOTE_UNCONFIRMED, NOTE_PAID_GUESS)


def utc_now():
    return datetime.now(timezone.utc)


class FinanceAccount(Base):
    """One balance the owner reads off a bank, wallet or cash drawer. held_cents is money already
    spoken for (a customer's parts deposit), so it isn't counted as spendable."""
    __tablename__ = "finance_accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False, unique=True)
    scope = Column(String(12), nullable=False, default="business")
    balance_cents = Column(Integer, nullable=False, default=0)
    held_cents = Column(Integer, nullable=False, default=0)
    note = Column(String(200))
    as_of = Column(Date, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class FinanceBill(Base):
    __tablename__ = "finance_bills"
    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    scope = Column(String(12), nullable=False, default="business")
    category = Column(String(16), nullable=False, default="other")
    amount_cents = Column(Integer, nullable=False)
    due_date = Column(Date, nullable=False)
    repeats = Column(String(8), nullable=False, default="once")
    status = Column(String(8), nullable=False, default="open")  # open, paid, skipped
    paid_on = Column(Date)
    note = Column(String(200))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (Index("ix_finance_bills_status_due", "status", "due_date"),)


class FinanceIncoming(Base):
    """Money the owner expects to collect: a customer balance, a deposit, a pending transfer."""
    __tablename__ = "finance_incoming"
    id = Column(Integer, primary_key=True)
    source = Column(String(80), nullable=False)
    scope = Column(String(12), nullable=False, default="business")
    amount_cents = Column(Integer, nullable=False)
    expected_date = Column(Date)
    status = Column(String(10), nullable=False, default="expected")  # expected, received, dropped
    received_on = Column(Date)
    note = Column(String(200))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (Index("ix_finance_incoming_status_date", "status", "expected_date"),)


class FinanceChange(Base):
    """Audit trail: who changed which money figure and when."""
    __tablename__ = "finance_changes"
    id = Column(Integer, primary_key=True)
    actor = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    detail = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
