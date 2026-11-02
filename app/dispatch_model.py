"""Operations queue persistence. Apply migrations/20261102_dispatcher.sql first."""
from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, text
from app.database import Base

OPEN_STATUSES = ("routine", "needs_owner")


def _now():
    return datetime.now().astimezone()


class DispatchItem(Base):
    """One open item per Quo conversation; later messages update it."""
    __tablename__ = "dispatch_items"
    id = Column(Integer, primary_key=True)
    quo_conversation_id = Column(String(64), nullable=False)
    customer_phone = Column(String(32), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"))
    vehicle_text = Column(String(120))
    category = Column(String(24), nullable=False, default="general")
    urgency = Column(String(12), nullable=False, default="normal")
    summary = Column(Text, nullable=False, default="")
    proposed_action = Column(Text, nullable=False, default="")
    approval_reasons = Column(Text, nullable=False, default="[]")
    status = Column(String(16), nullable=False, default="routine")
    last_message_id = Column(String(128), nullable=False)
    decided_by = Column(Integer, ForeignKey("users.id"))
    decided_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    __table_args__ = (
        Index("uq_dispatch_open_conversation", "quo_conversation_id", unique=True,
              postgresql_where=text("status IN ('routine','needs_owner')"),
              sqlite_where=text("status IN ('routine','needs_owner')")),
    )


class DispatchEvent(Base):
    """Each Quo message is processed once, however often it is delivered."""
    __tablename__ = "dispatch_events"
    id = Column(Integer, primary_key=True)
    quo_message_id = Column(String(128), nullable=False, unique=True)
    item_id = Column(Integer, ForeignKey("dispatch_items.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class DispatchAudit(Base):
    """Who or what did what. Holds no message text."""
    __tablename__ = "dispatch_audit"
    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("dispatch_items.id"), index=True)
    actor = Column(String(64), nullable=False)
    action = Column(String(32), nullable=False)
    detail = Column(String(255))
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)


class DispatchSync(Base):
    """Health of each feed into the queue, shown on the dashboard."""
    __tablename__ = "dispatch_sync"
    source = Column(String(32), primary_key=True)
    last_success_at = Column(DateTime(timezone=True))
    last_failure_at = Column(DateTime(timezone=True))
    consecutive_failures = Column(Integer, nullable=False, default=0)
    last_detail = Column(String(255))


class DispatchPolicy(Base):
    __tablename__ = "dispatch_policies"
    key = Column(String(32), primary_key=True)
    mode = Column(String(12), nullable=False, default="approve")
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
