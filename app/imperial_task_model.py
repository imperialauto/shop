"""Unified internal tasks for the Imperial assistant.

Separate from short-lived Quo communication items and confirmed appointments.
Production migration: migrations/20261106_imperial_tasks.sql (not auto-applied).
"""
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from app.database import Base


def utc_now():
    return datetime.now(timezone.utc)


class ImperialTask(Base):
    __tablename__ = "imperial_tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    details = Column(Text)
    source = Column(String(32), nullable=False)
    external_id = Column(String(128), nullable=False)
    project_key = Column(String(64))
    category = Column(String(32), nullable=False, default="internal")
    priority = Column(String(12), nullable=False, default="normal")
    status = Column(String(24), nullable=False, default="open")
    assigned_user_id = Column(Integer, ForeignKey("users.id"))
    due_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_imperial_task_source_event"),
        Index("ix_imperial_tasks_state_due", "status", "due_at"),
    )


class ImperialTaskAudit(Base):
    __tablename__ = "imperial_task_audit"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("imperial_tasks.id"), nullable=False, index=True)
    actor = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    event_id = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
