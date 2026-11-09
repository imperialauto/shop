"""Home screen numbers, on made-up records in a private database."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import calendar_feed, home_summary
from app.appointments import PHOENIX
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"


class HomeSummaryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            customer = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add(customer)
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2016, make="Ford", model="Focus")
            db.add(vehicle)
            db.flush()
            jobs = [RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status=s) for s in ("intake", "in_progress", "waiting_approval", "delivered")]
            db.add_all(jobs)
            db.flush()
            db.add(LineItem(repair_order_id=jobs[0].id, description="x", unit_price=10, approval="pending"))
            invoice = Invoice(repair_order_id=jobs[3].id, total=500, status="sent")
            db.add(invoice)
            db.flush()
            db.add(Payment(invoice_id=invoice.id, amount=200, paid_at=datetime.utcnow()))
            db.add(DispatchItem(quo_conversation_id="CN1", customer_phone="+16025550100", summary="x", category="repair",
                                urgency="safety", status="needs_owner", last_message_id="m1"))
            db.add(DispatchItem(quo_conversation_id="CN2", customer_phone="+16025550101", summary="y", category="repair",
                                urgency="normal", status="routine", last_message_id="m2"))
            db.commit()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def test_numbers(self):
        with self.Session() as db, patch.object(calendar_feed, "upcoming", return_value=[]):
            data = home_summary.summary(db, SimpleNamespace(role="owner"), datetime.now(PHOENIX))
        self.assertEqual((data["needs_you"], data["urgent"], data["awaiting"]), (1, 1, 2))
        money = dict(data["money"])
        self.assertEqual(money.pop("cycle")["days"], 30)  # first contact to paid, checked in test_cycle_time
        self.assertEqual(money, {"owed": 300, "owed_count": 1, "paid_week": 200})
        stages = {s["key"]: s["count"] for s in data["stages"]}
        self.assertEqual((stages["intake"], stages["in_progress"], stages["waiting_approval"]), (1, 1, 1))

    def test_money_is_owner_only_and_calendar_failure_is_shown(self):
        with self.Session() as db, patch.object(calendar_feed, "upcoming", return_value=None):
            data = home_summary.summary(db, SimpleNamespace(role="office"))
        self.assertIsNone(data["money"])
        self.assertFalse(data["calendar_ok"])

    def test_home_screen_renders_for_owner_and_office(self):
        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        with patch.object(calendar_feed, "upcoming", return_value=[]):
            for name, money in (("owner", True), ("office", False)):
                client = TestClient(app)
                client.post("/login", data={"username": name, "password": PASSWORD})
                page = client.get("/dashboard")
                self.assertEqual(page.status_code, 200)
                self.assertIn("Waiting on customer approval", page.text)
                self.assertEqual("paid this week" in page.text, money)


if __name__ == "__main__":
    unittest.main()
