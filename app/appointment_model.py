"""Scheduling persistence model. Import into app.models after applying migration."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, CheckConstraint, Numeric, UniqueConstraint
from app.database import Base

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False, index=True)
    resource_id = Column(String(80), nullable=False, index=True)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(24), nullable=False, default="tentative")
    # Quoted value at booking. This is not collected money.
    booked_value = Column(Numeric(10, 2), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone())
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="appointment_positive_duration"),
        CheckConstraint("status IN ('tentative','confirmed','cancelled','completed')", name="appointment_valid_status"),
        CheckConstraint("booked_value >= 0", name="appointment_booked_value_nonnegative"),
    )


class BookingSlot(Base):
    """A time the shop offers. It is open until an active appointment overlaps it."""
    __tablename__ = "booking_slots"
    id = Column(Integer, primary_key=True)
    resource_id = Column(String(80), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    ends_at = Column(DateTime(timezone=True), nullable=False)
    removed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now().astimezone())
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="booking_slot_positive_duration"),
    )
