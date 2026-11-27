"""Ask Imperial: staff requests answered on the dashboard, with AI work done by the Local Node.

Separate from diagnostic reviews on purpose:
* its own table and validator (app/assist_guard.py), so the diagnostic validator keeps
  rejecting labor and pricing output exactly as before;
* the same workstation worker, key, claim/lease/retry/heartbeat rules
  (app/routes/local_node.py). Assist tasks are only handed to a worker that reports
  version 1.2.0 or later, because older workers post every result to the diagnostic route.

The AI never supplies hours or prices. Labor comes from evidence an advisor enters after
checking ALLDATA Repair (LaborEvidence); parts from the advisor's AutoZonePro/O'Reilly check;
the total from Imperial's approved rates and tax (app/pricing.py, invoices.SHOP_TAX_RATE).

Production migration: migrations/20261116_ask_imperial.sql (not auto-applied).
"""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, Numeric,
                        String, Text, text)

from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


KINDS = ("estimate_draft",)
# queued -> processing -> (retry_wait ->) needs_labor | failed
# needs_labor: AI read the request; an advisor must verify labor in ALLDATA
# ready: labor evidence entered, draft priced, waiting for Apply
# applied: draft lines saved on the RO (final)
# stale: the RO scope or the customer changed after the draft was based; Apply refused
# cancelled: staff stopped it
ACTIVE = ("queued", "processing", "retry_wait")
OPEN = ACTIVE + ("needs_labor", "ready", "stale", "failed")
FINAL = ("applied", "cancelled")
STATUSES = OPEN + FINAL


class AssistRequest(Base):
    __tablename__ = "assist_requests"

    id = Column(Integer, primary_key=True)
    kind = Column(String(24), nullable=False, default="estimate_draft")
    status = Column(String(16), nullable=False, default="queued")
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"))
    request_text = Column(Text, nullable=False)          # what staff typed or dictated, corrected
    source_snapshot = Column(Text, nullable=False)       # JSON given to the AI; no customer contact details
    base_fingerprint = Column(String(64), nullable=False)  # RO scope when the draft was based
    base_customer_message = Column(String(128))          # newest customer message id at that time
    base_summary = Column(Text)                          # human-readable scope at that time (JSON)
    prompt_version = Column(String(32), nullable=False)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    lease_owner = Column(String(64))
    lease_token = Column(String(64))
    lease_until = Column(DateTime(timezone=True))
    retry_after = Column(DateTime(timezone=True))
    last_error = Column(Text)
    model_name = Column(String(96))
    ai_json = Column(Text)            # validated reading of the request (operations, questions)
    stale_reason = Column(Text)       # JSON list of what changed
    applied_line_ids = Column(Text)   # JSON list, set once on Apply
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind IN ('estimate_draft')", name="ck_assist_kind"),
        CheckConstraint(
            "status IN ('queued','processing','retry_wait','needs_labor','ready','stale','failed','applied','cancelled')",
            name="ck_assist_status"),
        CheckConstraint("attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10", name="ck_assist_attempts"),
        # One open estimate draft per job: a double tap or a second phone can't start two.
        Index("uq_assist_one_open_per_ro", "repair_order_id", unique=True,
              postgresql_where=text("status IN ('queued','processing','retry_wait','needs_labor','ready','stale','failed')"),
              sqlite_where=text("status IN ('queued','processing','retry_wait','needs_labor','ready','stale','failed')")),
        Index("ix_assist_status_created", "status", "created_at"),
    )


class AssistEvent(Base):
    """Append-only history: every request step, refused late result or stale Apply, and every
    internal note or task change made from Ask Imperial."""

    __tablename__ = "assist_events"

    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("assist_requests.id"), index=True)  # null for a note or task edit
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), index=True)
    actor = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class LaborEvidence(Base):
    """What the advisor verified. Kept even when the draft goes stale or is revised."""

    __tablename__ = "assist_labor_evidence"

    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("assist_requests.id"), nullable=False, index=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False)
    vehicle_config = Column(Text, nullable=False)     # engine/drive/trim the operation was looked up for
    operation = Column(Text, nullable=False)          # operation name as listed in the source
    source = Column(String(40), nullable=False)       # "ALLDATA Repair"
    guide_hours = Column(Numeric(6, 2), nullable=False)  # published guide time, as listed
    inclusions = Column(Text)                         # what the guide time includes
    overlap = Column(Text)                            # overlap with other operations on this job
    rate_type = Column(String(12), nullable=False, default="shop")
    verified_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    shop_manager_ref = Column(String(64))             # Shop Manager estimate reference for this RO
    parts_json = Column(Text, nullable=False, default="[]")  # vendor, part number, description, price, availability, checked_at
    superseded_by_id = Column(Integer, ForeignKey("assist_labor_evidence.id"))

    __table_args__ = (
        CheckConstraint("guide_hours > 0 AND guide_hours <= 100", name="ck_labor_hours_range"),
        CheckConstraint("rate_type IN ('shop','mobile','diesel','cspr')", name="ck_labor_rate_type"),
    )


class ShopRule(Base):
    """A correction staff chose to reuse ("Save as a shop rule"). Never created implicitly.
    Minimal store until the Knowledge & Memory Hub (register R29) exists."""

    __tablename__ = "shop_rules"

    id = Column(Integer, primary_key=True)
    text = Column(Text, nullable=False)
    topic = Column(String(40), nullable=False, default="estimates")
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    from_request_id = Column(Integer, ForeignKey("assist_requests.id"))
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
