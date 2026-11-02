"""Scheduling persistence model. Import into app.models after applying migration."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, CheckConstraint, UniqueConstraint
from app.database import Base

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False, index=True)
    resource_id = Column(String(80), nullable=False, index=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(24), nullable=False, default="tentative")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone())
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="appointment_positive_duration"),
        CheckConstraint("status IN ('tentative','confirmed','cancelled','completed')", name="appointment_valid_status"),
    )
