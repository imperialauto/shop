"""Persistent development work orders for Imperial's owner-only SMS coordinator.

These are not repair orders, customer contacts, or outgoing communications.
No AI is invoked by creating a job; future workers need their own approvals,
quota controls and separate service credentials.
"""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class DevWorkJob(Base):
    __tablename__ = "dev_work_jobs"

    id = Column(Integer, primary_key=True)
    source = Column(String(32), nullable=False)
    external_id = Column(String(128), nullable=False)
    project_key = Column(String(32), nullable=False)
    instruction = Column(Text, nullable=False)
    model_preference = Column(String(16), nullable=False, default="auto")
    status = Column(String(24), nullable=False, default="queued")
    assigned_worker = Column(String(64))
    attempt_count = Column(Integer, nullable=False, default=0)
    lease_until = Column(DateTime(timezone=True))
    retry_after = Column(DateTime(timezone=True))
    latest_summary = Column(Text)
    result_url = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_dev_work_source_external"),
        Index("ix_dev_work_status_created", "status", "created_at"),
    )


class DevWorkEvent(Base):
    __tablename__ = "dev_work_events"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("dev_work_jobs.id"), nullable=False, index=True)
    actor = Column(String(32), nullable=False)
    action = Column(String(32), nullable=False)
    detail = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class DevWorkNotice(Base):
    """At-most-once owner-only notification: never automatically retry claims."""

    __tablename__ = "dev_work_notices"
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("dev_work_events.id"), nullable=False, unique=True)
    job_id = Column(Integer, ForeignKey("dev_work_jobs.id"), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="pending")
    text = Column(Text, nullable=False)
    claim_nonce = Column(String(64))
    claimed_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
