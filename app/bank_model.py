"""Bank feed: banks the owner linked through Plaid, their accounts, and the transactions pulled in.

The owner signs in to each bank once in Plaid's own window; this app never sees a bank password.
Plaid's access token for each bank is stored encrypted. Amounts are whole cents, money in positive.
Production migration: migrations/20261123_bank_feed.sql (not auto-applied). The readiness check
only expects these tables once Plaid keys are set (app/recovery.py).
"""
from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text

from app.database import Base
from app.finance_model import utc_now

BANK_TABLES = ("bank_links", "bank_accounts", "bank_transactions")
LINK_STATUSES = ("ok", "needs_login", "error")


class BankLink(Base):
    """One bank login linked through Plaid (Plaid calls it an Item)."""
    __tablename__ = "bank_links"
    id = Column(Integer, primary_key=True)
    item_id = Column(String(100), nullable=False, unique=True)
    institution = Column(String(120), nullable=False, default="Bank")
    access_token_enc = Column(Text, nullable=False)
    cursor = Column(Text)
    status = Column(String(16), nullable=False, default="ok")
    last_synced_at = Column(DateTime(timezone=True))
    last_error = Column(String(200))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class BankAccount(Base):
    """One account at a linked bank. A cash account feeds the Money page balance it's tied to."""
    __tablename__ = "bank_accounts"
    id = Column(Integer, primary_key=True)
    link_id = Column(Integer, nullable=False, index=True)
    plaid_account_id = Column(String(100), nullable=False, unique=True)
    name = Column(String(120), nullable=False)
    mask = Column(String(8))
    kind = Column(String(24), nullable=False, default="depository")
    subtype = Column(String(32))
    finance_account_id = Column(Integer)
    scope = Column(String(12), nullable=False, default="business")
    tracked = Column(Boolean, nullable=False, default=True)
    current_cents = Column(Integer)
    available_cents = Column(Integer)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"
    id = Column(Integer, primary_key=True)
    plaid_transaction_id = Column(String(100), nullable=False, unique=True)
    bank_account_id = Column(Integer, nullable=False, index=True)
    day = Column(Date, nullable=False, index=True)
    name = Column(String(160), nullable=False)
    amount_cents = Column(Integer, nullable=False)  # money in positive, money out negative
    pending = Column(Boolean, nullable=False, default=False)
    category = Column(String(60))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
