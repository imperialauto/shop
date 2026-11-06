"""Persistent service opportunities; separate from short-lived communication tasks.

Migration: migrations/20261106_service_leads.sql. Feature-gated until applied.
No historical customer or repair-order rows are modified by creating this table.
"""
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, text
from app.database import Base

ACTIVE_LEAD_STATES = (
    "new", "information_needed", "estimate_needed", "estimate_sent",
    "awaiting_customer", "scheduling",
)
FINAL_LEAD_STATES = ("booked", "declined", "closed_exception")


def utc_now():
    return datetime.now(timezone.utc)


class ServiceLead(Base):
    __tablename__ = "service_leads"
    id = Column(Integer, primary_key=True)
    source = Column(String(20), nullable=False)
    external_thread_id = Column(String(128), nullable=False)
    customer_phone = Column(String(32))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"))
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"))
    appointment_id = Column(Integer, ForeignKey("appointments.id"))
    last_dispatch_item_id = Column(Integer, ForeignKey("dispatch_items.id"))
    assigned_user_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String(32), nullable=False, default="new")
    category = Column(String(32), nullable=False, default="general")
    urgency = Column(String(12), nullable=False, default="normal")
    summary = Column(Text, nullable=False, default="")
    next_action = Column(Text, nullable=False, default="")
    next_action_due_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_customer_activity_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_event_id = Column(String(128))
    closure_reason = Column(String(128))
    closure_evidence_ref = Column(String(128))
    closed_by_user_id = Column(Integer, ForeignKey("users.id"))
    resolved_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    __table_args__ = (
        Index("uq_service_leads_active_source_thread", "source", "external_thread_id",
              unique=True, postgresql_where=text("status IN ('new','information_needed','estimate_needed','estimate_sent','awaiting_customer','scheduling')"),
              sqlite_where=text("status IN ('new','information_needed','estimate_needed','estimate_sent','awaiting_customer','scheduling')")),
        Index("ix_service_leads_follow_up", "status", "next_action_due_at"),
    )


class ServiceLeadAudit(Base):
    """Status and ownership history only; never stores raw transcript contents."""
    __tablename__ = "service_lead_audit"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("service_leads.id"), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    actor = Column(String(64), nullable=False)
    from_status = Column(String(32))
    to_status = Column(String(32))
    evidence_ref = Column(String(128))
    at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
