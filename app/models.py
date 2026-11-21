from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Enum, LargeBinary
from sqlalchemy.orm import relationship
from app.database import Base
import enum


class CustomerType(str, enum.Enum):
    individual = "individual"
    fleet = "fleet"


class ROStatus(str, enum.Enum):
    intake = "intake"
    diagnosing = "diagnosing"
    waiting_parts = "waiting_parts"
    in_progress = "in_progress"
    waiting_approval = "waiting_approval"
    approval_received = "approval_received"
    complete = "complete"
    delivered = "delivered"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    full_name = Column(String(128))
    role = Column(String(32), default="tech")
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    must_change_password = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    customer_type = Column(String(16), default="individual")
    fleet_name = Column(String(128))
    first_name = Column(String(64), nullable=False)
    last_name = Column(String(64), nullable=False)
    email = Column(String(128))
    phone = Column(String(32))
    address = Column(String(256))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    name_source = Column(String(24))  # staff, quo_contact, text, call, call summary; empty on older records
    name_checked_at = Column(DateTime(timezone=True))  # last time Imperial looked in Quo for a name

    vehicles = relationship("Vehicle", back_populates="customer")
    repair_orders = relationship("RepairOrder", back_populates="customer")


class Vehicle(Base):
    __tablename__ = "vehicles"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    year = Column(Integer)
    make = Column(String(64))
    model = Column(String(128))
    trim = Column(String(64))
    engine = Column(String(64))
    vin = Column(String(17))
    license_plate = Column(String(16))
    mileage = Column(Integer)
    color = Column(String(32))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="vehicles")
    repair_orders = relationship("RepairOrder", back_populates="vehicle")

    @property
    def label(self):
        """Year make model for screens; skips blanks, "None" and "Unknown"."""
        parts = [str(p).strip() for p in (self.year, self.make, self.model) if p is not None]
        parts = [p for p in parts if p and p.lower() not in ("none", "unknown")]
        return " ".join(parts) or "Vehicle details not given yet"


class RepairOrder(Base):
    __tablename__ = "repair_orders"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=False)
    assigned_tech_id = Column(Integer, ForeignKey("users.id"))
    status = Column(String(32), default="intake")
    concern = Column(Text)
    tech_notes = Column(Text)
    public_token = Column(String(64), unique=True)
    mileage_in = Column(Integer)
    mileage_out = Column(Integer)
    promised_date = Column(DateTime)
    # Customer signature (estimate approval)
    signature_data = Column(Text, nullable=True)   # base64 PNG data URL
    approved_by = Column(String(128), nullable=True)
    signed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = relationship("Customer", back_populates="repair_orders")
    vehicle = relationship("Vehicle", back_populates="repair_orders")
    assigned_tech = relationship("User")
    line_items = relationship("LineItem", back_populates="repair_order")
    invoices = relationship("Invoice", back_populates="repair_order")
    communications = relationship("Communication", back_populates="repair_order")


class LineItem(Base):
    __tablename__ = "line_items"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False)
    description = Column(Text, nullable=False)
    item_type = Column(String(16), default="labor")  # labor, part, sublet, misc
    quantity = Column(Float, default=1.0)
    unit_price = Column(Float, default=0.0)
    taxable = Column(Boolean, default=True)
    approval = Column(String(12))  # None: not sent for approval. pending, approved, declined
    decided_at = Column(DateTime(timezone=True))
    # How a yes or no was recorded: None or "link" when the customer used their approval link,
    # "in_person" or "phone" when shop staff recorded it, with the staff login that did.
    approval_source = Column(String(16))
    approval_recorded_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    # Technician completion. Separate from customer approval, invoicing and payment.
    completed_at = Column(DateTime(timezone=True))
    completed_by = Column(String(64))

    repair_order = relationship("RepairOrder", back_populates="line_items")

    @property
    def workable(self):
        """Work can be marked done only on lines that don't need, or already have, customer approval."""
        return self.approval in (None, "approved")

    @property
    def total(self):
        return self.quantity * self.unit_price

    @property
    def billable(self):
        """Declined or still-pending lines are never billed."""
        return self.approval in (None, "approved")


class RoAttachment(Base):
    """A photo or file on a repair order, optionally tied to one line. Internal unless marked for the customer."""
    __tablename__ = "ro_attachments"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False, index=True)
    line_item_id = Column(Integer, ForeignKey("line_items.id", ondelete="SET NULL"))
    filename = Column(String(200), nullable=False)
    content_type = Column(String(64), nullable=False)
    size = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)
    visibility = Column(String(12), nullable=False, default="internal")  # internal, customer
    uploaded_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class PartsOrder(Base):
    __tablename__ = "parts_orders"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"))
    vendor = Column(String(128))
    part_number = Column(String(64))
    description = Column(Text)
    quantity = Column(Integer, default=1)
    cost = Column(Float, default=0.0)
    status = Column(String(32), default="ordered")  # ordered, received, returned
    ordered_at = Column(DateTime, default=datetime.utcnow)
    received_at = Column(DateTime)


class Communication(Base):
    __tablename__ = "communications"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"))
    customer_id = Column(Integer, ForeignKey("customers.id"))
    direction = Column(String(8), default="outbound")  # inbound, outbound
    channel = Column(String(16), default="sms")  # sms, email, phone, messenger
    body = Column(Text)
    external_id = Column(String(128))
    created_at = Column(DateTime, default=datetime.utcnow)

    repair_order = relationship("RepairOrder", back_populates="communications")


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=False)
    subtotal = Column(Float, default=0.0)
    tax_rate = Column(Float, default=0.0875)
    tax_amount = Column(Float, default=0.0)
    total = Column(Float, default=0.0)
    status = Column(String(16), default="draft")  # draft, sent, paid, void
    notes = Column(Text)
    # Customer signature at completion/pickup
    customer_signature = Column(Text, nullable=True)  # base64 PNG data URL
    customer_approved_by = Column(String(128), nullable=True)
    customer_signed_at = Column(DateTime, nullable=True)
    # Balance the owner chose not to collect. Not a payment, so it never counts as money received.
    waived_amount = Column(Float, nullable=False, default=0.0, server_default="0")
    waived_reason = Column(Text)
    waived_by = Column(String(64))
    waived_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime, default=datetime.utcnow)

    repair_order = relationship("RepairOrder", back_populates="invoices")
    payments = relationship("Payment", back_populates="invoice")

    @property
    def paid_total(self):
        return round(sum(p.amount or 0 for p in self.payments), 2)

    @property
    def balance(self):
        """What the customer still owes: total less payments and any waived amount."""
        return round((self.total or 0) - self.paid_total - (self.waived_amount or 0), 2)


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String(32))  # cash, card, check, ach
    reference = Column(String(128))
    paid_at = Column(DateTime, default=datetime.utcnow)

    invoice = relationship("Invoice", back_populates="payments")


class EstimateSession(Base):
    """Tracks an active SMS estimate intake conversation."""
    __tablename__ = "estimate_sessions"
    id = Column(Integer, primary_key=True)
    phone_number = Column(String(32), nullable=False, index=True)
    status = Column(String(32), default="collecting")  # collecting, generating, draft_ready, error, closed
    conversation = Column(Text, default="[]")       # JSON list of {role, content} messages
    collected_data = Column(Text, default="{}")     # JSON with extracted vehicle/complaint info
    draft_ro_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=True)
    follow_up_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
