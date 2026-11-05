"""Creates a throwaway SQLite database with made-up data for the app walkthrough."""
import secrets
from datetime import datetime, timedelta, timezone

from app.appointment_model import Appointment  # noqa: F401
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.dispatch_model import DispatchItem
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, User, Vehicle

Base.metadata.create_all(engine)
db = SessionLocal()
db.add(User(id=1, username="walkthrough", full_name="Test Owner", password_hash=hash_password("walkthrough-only-pass"), role="owner"))
db.add(User(id=2, username="tech", full_name="Test Tech", password_hash=hash_password("walkthrough-only-pass"), role="tech"))
for n, (first, last) in enumerate([("Pat", "Sample"), ("Lee", "Testcase"), ("Sam", "Example"), ("Alex", "Placeholder")], 1):
    db.add(Customer(id=n, first_name=first, last_name=last, phone=f"602555010{n}", email=f"{first.lower()}@example.com"))
    db.add(Vehicle(id=n, customer_id=n, year=2015 + n, make="Ford", model="Fusion", vin=f"TESTVIN000000000{n}", mileage=90000 + n))
statuses = ["intake", "diagnosing", "waiting_parts", "in_progress", "waiting_approval", "complete"]
for n, status in enumerate(statuses, 1):
    customer = (n - 1) % 4 + 1
    db.add(RepairOrder(id=n, customer_id=customer, vehicle_id=customer, status=status, concern=f"Sample concern number {n} with a longer description to test wrapping on a phone screen", public_token=secrets.token_urlsafe(16)))
    db.add(LineItem(repair_order_id=n, description="Sample labor", item_type="labor", quantity=2, unit_price=150))
    db.add(LineItem(repair_order_id=n, description="Sample part", item_type="part", quantity=1, unit_price=89.99))
db.add(Invoice(id=1, repair_order_id=6, subtotal=389.99, tax_amount=34.12, total=424.11, status="sent"))
db.add(Invoice(id=2, repair_order_id=5, subtotal=389.99, tax_amount=34.12, total=424.11, status="paid"))
db.add(Payment(invoice_id=2, amount=424.11, method="cash"))
now = datetime.now(timezone.utc)
for n, (status, urgency, category, summary, draft) in enumerate([
    ("needs_owner", "urgent", "arrival", "I'm here", "Thanks, we'll be right with you."),
    ("needs_owner", "normal", "pricing", "How much for brakes on a 2019 Ford Edge?", "We can look at that today."),
    ("needs_owner", "safety", "repair", "Fuel leak and strong smell", None),
    ("approved", "normal", "scheduling", "Can I come Tuesday?", "Yes, Tuesday works."),
    ("routine", "normal", "general", "Thanks", ""),
], 1):
    db.add(DispatchItem(id=n, quo_conversation_id=f"CNsample{n}", last_message_id=f"MSGsample{n}", customer_phone=f"+1602555010{n}", status=status, urgency=urgency,
                        category=category, summary=summary, proposed_action="Review the message and reply.", draft_reply=draft,
                        approval_reasons='["customer_reply"]', contact_name=f"Sample Contact {n}", updated_at=now - timedelta(hours=n), created_at=now - timedelta(hours=n)))
db.commit()
if engine.dialect.name == "postgresql":
    from sqlalchemy import text
    for table in ("users", "customers", "vehicles", "repair_orders", "invoices", "dispatch_items"):
        db.execute(text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))"))
    db.commit()
db.close()
print("seeded")
