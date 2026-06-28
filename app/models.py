from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, Float, Boolean, DateTime, ForeignKey, Enum
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
    complete = "complete"
    delivered = "delivered"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    full_name = Column(String(128))
    role = Column(String(32), default="tech")
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
    created_at = Column(DateTime, default=datetime.utcnow)

    repair_order = relationship("RepairOrder", back_populates="line_items")

    @property
    def total(self):
        return self.quantity * self.unit_price


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
    created_at = Column(DateTime, default=datetime.utcnow)

    repair_order = relationship("RepairOrder", back_populates="invoices")
    payments = relationship("Payment", back_populates="invoice")


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    amount = Column(Float, nullable=False)
    method = Column(String(32))  # cash, card, check, ach
    reference = Column(String(128))
    paid_at = Column(DateTime, default=datetime.utcnow)

    invoice = relationship("Invoice", back_populates="payments")
