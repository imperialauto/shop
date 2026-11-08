"""Made-up records for a disposable preview. Runs only in preview mode, and only on an empty database."""
import os
import secrets
from datetime import datetime, timedelta

from app.appointment_model import Appointment, BookingSlot  # noqa: F401
from app.appointments import PHOENIX
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.dispatch_model import DispatchAudit  # noqa: F401
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.public_site import preview_mode
from app.security import UserMfa  # noqa: F401

SLOT_HOURS = {5: (12, 14, 15), **{day: (11, 13, 15, 17) for day in range(5)}}


def seed() -> bool:
    password = os.getenv("PREVIEW_OWNER_PASSWORD", "")
    if not preview_mode() or len(password) < 12:
        return False
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        owner = db.query(User).filter(User.username == "preview-owner").first()
        if owner is not None:  # lets a test password be replaced without rebuilding the data
            owner.password_hash = hash_password(password)
            db.commit()
            return False
        db.add(User(username="preview-owner", full_name="Preview Owner", password_hash=hash_password(password), role="owner"))
        for n, (first, last) in enumerate([("Pat", "Sample"), ("Lee", "Testcase"), ("Sam", "Example")], 1):
            customer = Customer(first_name=first, last_name=last, phone=f"(602) 555-010{n}", email=f"{first.lower()}@example.com")
            db.add(customer)
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2015 + n, make="Ford", model="Fusion", vin=f"TESTVIN000000000{n}", mileage=90000 + n)
            db.add(vehicle)
            db.flush()
            order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="in_progress",
                                concern=f"Made-up preview job {n}.", public_token=secrets.token_urlsafe(32))
            db.add(order)
            db.flush()
            db.add(LineItem(repair_order_id=order.id, description="Sample labor", item_type="labor", quantity=1, unit_price=0))
        today = datetime.now(PHOENIX).replace(minute=0, second=0, microsecond=0)
        for offset in range(1, 15):
            day = today + timedelta(days=offset)
            for hour in SLOT_HOURS.get(day.weekday(), ()):
                start = day.replace(hour=hour)
                db.add(BookingSlot(resource_id="bay-1", starts_at=start, ends_at=start + timedelta(hours=1)))
        db.commit()
    return True
