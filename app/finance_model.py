"""Finance tables. Source columns are written once at import and never edited;
everything an owner or the engine decides lives in the analysis columns."""
from datetime import datetime
from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Index, Integer, String, Text
from app.database import Base


class FinAccount(Base):
    __tablename__ = "fin_accounts"
    id = Column(Integer, primary_key=True)
    institution = Column(String(64), nullable=False)
    last4 = Column(String(4), nullable=False, unique=True)  # never a full number
    name = Column(String(96), nullable=False)
    designation = Column(String(16), nullable=False, default="business")  # business|personal|mixed
    status = Column(String(16), nullable=False, default="unknown")  # active|closed|unknown
    in_scope = Column(Boolean, nullable=False, default=True)


class FinImportBatch(Base):
    __tablename__ = "fin_import_batches"
    id = Column(Integer, primary_key=True)
    filename = Column(String(200), nullable=False)
    kind = Column(String(24), nullable=False)
    sha256 = Column(String(64), nullable=False)
    rows_read = Column(Integer, nullable=False, default=0)
    rows_added = Column(Integer, nullable=False, default=0)
    rows_skipped = Column(Integer, nullable=False, default=0)
    imported_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class FinStatement(Base):
    __tablename__ = "fin_statements"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("fin_accounts.id"), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    beginning_cents = Column(BigInteger, nullable=False)
    deposits_cents = Column(BigInteger, nullable=False)
    withdrawals_cents = Column(BigInteger, nullable=False)  # positive magnitude, fees included
    ending_cents = Column(BigInteger, nullable=False)
    source_file = Column(String(200))
    batch_id = Column(Integer, ForeignKey("fin_import_batches.id"))
    __table_args__ = (Index("uq_fin_statement_period", "account_id", "period_start", "period_end", unique=True),)


class FinTransaction(Base):
    __tablename__ = "fin_transactions"
    id = Column(Integer, primary_key=True)
    # Source: immutable after import.
    import_key = Column(String(64), nullable=False, unique=True)
    batch_id = Column(Integer, ForeignKey("fin_import_batches.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("fin_accounts.id"), nullable=False)
    source_type = Column(String(16), nullable=False)  # statement|ocr|bank_csv
    source_txn_id = Column(String(96))
    posted_date = Column(Date, nullable=False)
    amount_cents = Column(BigInteger, nullable=False)  # signed: money in is positive
    description = Column(Text, nullable=False)
    source_file = Column(String(200))
    source_page = Column(Integer)
    statement_period = Column(String(64))
    source_category = Column(String(96))
    source_issue = Column(String(200))
    # Analysis: recomputed unless locked by an owner decision.
    report_group = Column(String(24), nullable=False, default="unclassified")
    category = Column(String(64), nullable=False, default="Unclassified")
    designation = Column(String(16), nullable=False, default="unknown")
    division = Column(String(16), nullable=False, default="unassigned")
    confidence = Column(String(16), nullable=False, default="review")
    needs_review = Column(Boolean, nullable=False, default=False)
    review_reason = Column(String(200))
    duplicate_status = Column(String(20), nullable=False, default="unique")
    duplicate_of_id = Column(Integer, ForeignKey("fin_transactions.id"))
    transfer_status = Column(String(20), nullable=False, default="none")
    transfer_match_id = Column(Integer, ForeignKey("fin_transactions.id"))
    verification_status = Column(String(24), nullable=False, default="unverified")
    locked = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        Index("ix_fin_txn_date", "posted_date"),
        Index("ix_fin_txn_account_date", "account_id", "posted_date"),
        Index("ix_fin_txn_review", "needs_review"),
    )


class FinRule(Base):
    """Owner-held classification rules. Names of customers and people live here,
    in the database, and never in this public repository."""
    __tablename__ = "fin_rules"
    id = Column(Integer, primary_key=True)
    pattern = Column(String(200), nullable=False, unique=True)
    report_group = Column(String(24), nullable=False)
    category = Column(String(64), nullable=False)
    designation = Column(String(16), nullable=False, default="business")
    division = Column(String(16), nullable=False, default="unassigned")
    needs_review = Column(Boolean, nullable=False, default=False)
    note = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class FinReview(Base):
    __tablename__ = "fin_reviews"
    id = Column(Integer, primary_key=True)
    transaction_id = Column(Integer, ForeignKey("fin_transactions.id"), nullable=False, index=True)
    actor = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    before_value = Column(Text)
    after_value = Column(Text)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class FinAudit(Base):
    __tablename__ = "fin_audit"
    id = Column(Integer, primary_key=True)
    actor = Column(String(64))
    action = Column(String(48), nullable=False)
    detail = Column(String(300))
    ip = Column(String(64))
    allowed = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
