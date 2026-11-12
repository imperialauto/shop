"""Imperial Local Node: AI diagnostic reviews processed on the shop workstation.

The workstation never connects to the database. It reaches Railway over HTTPS
with its own narrowly scoped key (see app/routes/local_node.py) and can only
claim a queued review, report a draft, or report an error. Approval is a staff
action in the dashboard, enforced here by CHECK constraints and, on PostgreSQL,
by the trigger in migrations/20261111_local_ai_node.sql.

Production migration: migrations/20261111_local_ai_node.sql (not auto-applied).
"""
from datetime import datetime, timezone

from sqlalchemy import (CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text,
                        text)

from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


# queued      -> waiting for the workstation
# processing  -> claimed under a lease; lease_token must match to report back
# retry_wait  -> failed attempt, will be offered again after retry_after
# draft_ready -> AI draft passed validation; awaiting technician review
# failed      -> gave up after max attempts (staff can re-queue)
# approved / rejected -> final technician decision; row is then immutable
ACTIVE_STATUSES = ("queued", "processing", "retry_wait")
FINAL_STATUSES = ("approved", "rejected", "cancelled")
ALL_STATUSES = ACTIVE_STATUSES + ("draft_ready", "failed") + FINAL_STATUSES


class DiagnosticReview(Base):
    __tablename__ = "diagnostic_reviews"

    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"))
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    status = Column(String(16), nullable=False, default="queued")
    # Exactly what the AI was given, frozen at request time (JSON). No customer
    # name, phone, email or address is ever placed in it.
    source_snapshot = Column(Text, nullable=False)
    prompt_version = Column(String(32), nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    lease_owner = Column(String(64))
    lease_token = Column(String(64))
    lease_until = Column(DateTime(timezone=True))
    retry_after = Column(DateTime(timezone=True))
    last_error = Column(Text)
    model_name = Column(String(96))
    draft_json = Column(Text)          # validated structured draft
    validation_notes = Column(Text)    # JSON list of non-blocking cautions
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime(timezone=True))
    approved_text = Column(Text)
    decision_note = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','processing','retry_wait','draft_ready','failed','approved','rejected','cancelled')",
            name="ck_diag_review_status"),
        # A decision cannot exist without a named reviewer and time.
        CheckConstraint(
            "status NOT IN ('approved','rejected') OR (reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)",
            name="ck_diag_review_reviewer"),
        # Approval requires the technician-edited text that is saved to the job.
        CheckConstraint(
            "status <> 'approved' OR (approved_text IS NOT NULL AND length(approved_text) > 0)",
            name="ck_diag_review_approved_text"),
        CheckConstraint("attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10", name="ck_diag_review_attempts"),
        # One in-flight review per job: duplicate clicks or two tabs cannot queue twice.
        Index("uq_diag_review_one_active_per_ro", "repair_order_id", unique=True,
              postgresql_where=text("status IN ('queued','processing','retry_wait')"),
              sqlite_where=text("status IN ('queued','processing','retry_wait')")),
        Index("ix_diag_review_status_created", "status", "created_at"),
    )


class DiagnosticReviewEvent(Base):
    """Append-only audit history for each review."""

    __tablename__ = "diagnostic_review_events"

    id = Column(Integer, primary_key=True)
    review_id = Column(Integer, ForeignKey("diagnostic_reviews.id"), nullable=False, index=True)
    actor = Column(String(64), nullable=False)   # "user:<id>" or "node:<worker id>" or "system"
    action = Column(String(32), nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class LocalNodeHeartbeat(Base):
    """Last check-in per workstation; used only to show online/offline."""

    __tablename__ = "local_node_heartbeats"

    worker_id = Column(String(64), primary_key=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    version = Column(String(32))
    model_name = Column(String(96))
    ollama_ok = Column(String(8))        # "yes" / "no"
    detail = Column(Text)
