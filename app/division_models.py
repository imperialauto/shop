"""NVZN Customs division domain model. Isolated until migration and routes are reviewed."""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Numeric, DateTime, ForeignKey, Text, Boolean
from app.database import Base

class DivisionService(Base):
    __tablename__ = "division_services"
    id = Column(Integer, primary_key=True)
    division = Column(String(50), nullable=False, default="nvzn")
    name = Column(String(160), nullable=False)
    category = Column(String(80), nullable=False)
    retail_price = Column(Numeric(10,2), nullable=False)
    estimated_hours = Column(Numeric(8,2), nullable=False, default=0)
    material_cost = Column(Numeric(10,2), nullable=False, default=0)
    contractor_rate = Column(Numeric(5,4), nullable=False, default=.30)
    active = Column(Boolean, nullable=False, default=True)

class DivisionJob(Base):
    __tablename__ = "division_jobs"
    id = Column(Integer, primary_key=True)
    division = Column(String(50), nullable=False, default="nvzn")
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id"), nullable=True)
    service_id = Column(Integer, ForeignKey("division_services.id"), nullable=True)
    status = Column(String(32), nullable=False, default="intake")
    price = Column(Numeric(10,2), nullable=False, default=0)
    material_cost = Column(Numeric(10,2), nullable=False, default=0)
    contractor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    contractor_share = Column(Numeric(5,4), nullable=False, default=0)
    approved = Column(Boolean, nullable=False, default=False)
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class DivisionTimeEntry(Base):
    __tablename__ = "division_time_entries"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("division_jobs.id"), nullable=False)
    worker_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    stopped_at = Column(DateTime)
    note = Column(Text)

class DivisionPayout(Base):
    __tablename__ = "division_payouts"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("division_jobs.id"), nullable=False)
    worker_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount = Column(Numeric(10,2), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    approved_at = Column(DateTime)
    paid_at = Column(DateTime)

class DivisionAgreement(Base):
    __tablename__ = "division_agreements"
    id = Column(Integer, primary_key=True)
    division = Column(String(50), nullable=False, default="nvzn")
    worker_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    version = Column(String(40), nullable=False)
    document_location = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="draft")
    signed_at = Column(DateTime)
