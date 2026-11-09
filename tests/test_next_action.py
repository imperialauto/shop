"""Next step for a job, from intake to picked up. Made-up data, private database."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import next_action
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"


class NextActionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        with self.Session() as db:
            db.add_all([User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"),
                        User(id=2, username="tech", password_hash=hash_password(PASSWORD), role="tech"),
                        Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                        Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"),
                        RepairOrder(id=5, customer_id=1, vehicle_id=1, status="intake", public_token="t" * 20)])
            db.commit()

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def step(self, role="owner"):
        with self.Session() as db:
            return next_action.for_job(db, db.get(RepairOrder, 5), role)

    def change(self, fn):
        with self.Session() as db:
            fn(db, db.get(RepairOrder, 5))
            db.commit()

    def test_walks_the_job_from_intake_to_picked_up(self):
        self.assertEqual(self.step()["title"], "Add the repairs and prices")
        self.change(lambda db, ro: db.add(LineItem(repair_order_id=5, description="Pads", unit_price=349, item_type="labor")))
        self.assertEqual(self.step()["post"], "/ro/5/request-approval")
        self.assertIsNone(self.step("tech"))  # techs never ask customers for approval
        self.change(lambda db, ro: setattr(ro.line_items[0], "approval", "pending"))
        self.assertTrue(self.step()["title"].startswith("Waiting on the customer"))
        self.change(lambda db, ro: db.add(DispatchItem(quo_conversation_id="LINK5", customer_phone="+16025550100", category="approval_link",
                                                        summary="x", last_message_id="none", status="needs_owner")))
        self.assertIn("waiting for your YES", self.step()["title"])
        self.change(lambda db, ro: setattr(ro.line_items[0], "approval", "approved"))
        self.assertEqual(self.step()["button"], "Choose tech")
        self.change(lambda db, ro: setattr(ro, "assigned_tech_id", 2))
        self.assertEqual((self.step("tech")["button"], self.step("tech")["fields"]), ("Start work", {"status": "in_progress"}))
        self.change(lambda db, ro: setattr(ro, "status", "in_progress"))
        self.assertEqual(self.step("tech")["fields"], {"status": "complete"})
        self.change(lambda db, ro: setattr(ro, "status", "complete"))
        self.assertEqual(self.step()["post"], "/invoices/create/5")
        self.change(lambda db, ro: db.add(Invoice(id=9, repair_order_id=5, total=349.0, status="sent")))
        self.assertEqual((self.step()["title"], self.step()["href"]), ("Collect $349.00", "/invoices/9"))
        self.change(lambda db, ro: db.add(Payment(invoice_id=9, amount=349.0, method="card")))
        self.assertEqual(self.step()["fields"], {"status": "delivered"})
        self.assertIsNone(self.step("tech"))  # the front desk hands over the keys
        self.change(lambda db, ro: setattr(ro, "status", "delivered"))
        self.assertIsNone(self.step())

    def test_all_declined_is_flagged_not_invoiced(self):
        self.change(lambda db, ro: (db.add(LineItem(repair_order_id=5, description="Rotors", unit_price=200, approval="declined")),
                                    setattr(ro, "status", "complete")))
        self.assertEqual(self.step()["title"], "Nothing approved to bill")

    def test_stuck_jobs_text_once_a_day_and_respects_the_off_switch(self):
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        from app import alert_rules
        from app.routes import dispatch
        old = datetime(2026, 11, 2, 17, 0)
        self.change(lambda db, ro: (setattr(ro, "created_at", old), setattr(ro, "updated_at", old),
                                    db.add(LineItem(repair_order_id=5, description="Pads", unit_price=349, created_at=old))))
        now = datetime(2026, 11, 9, 11, 30, tzinfo=ZoneInfo("America/Phoenix"))
        with self.Session() as db:
            rows = next_action.stuck_jobs(db, now.astimezone(timezone.utc))
            self.assertEqual([r[0].id for r in rows], [5])
            text = next_action.stuck_text(rows)
        self.assertIn("#5 Pat Ford Focus: Ask the customer to approve 1 item (7d)", text)
        with self.Session() as db, patch.object(dispatch, "OWNER_PHONE", "+16025550199"), \
                patch.object(alert_rules, "rules", lambda: {**alert_rules.DEFAULTS, "off": []}):
            keys = [m["key"] for m in dispatch.due_owner_messages(db, now)]
            self.assertIn("stuck:2026-11-09", keys)
            self.assertNotIn("stuck:2026-11-09", [m["key"] for m in dispatch.due_owner_messages(db, now)])  # reserved once
        with self.Session() as db, patch.object(dispatch, "OWNER_PHONE", "+16025550199"), \
                patch.object(alert_rules, "rules", lambda: {**alert_rules.DEFAULTS, "off": ["owner_stuck"]}):
            later = now + timedelta(days=1)
            self.assertNotIn(f"stuck:{later.date()}", [m["key"] for m in dispatch.due_owner_messages(db, later)])
        # Fresh activity takes it off the list.
        self.change(lambda db, ro: setattr(ro, "updated_at", datetime(2026, 11, 9, 17, 0)))
        with self.Session() as db:
            self.assertEqual(next_action.stuck_jobs(db, now.astimezone(timezone.utc)), [])

    def test_job_page_shows_one_button(self):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": "owner", "password": PASSWORD})
        self.change(lambda db, ro: db.add(LineItem(repair_order_id=5, description="Pads", unit_price=349)))
        page = client.get("/ro/5").text
        self.assertIn('id="next-step"', page)
        self.assertIn('action="/ro/5/request-approval"', page)
        self.assertIn("Ask for approval", page)


if __name__ == "__main__":
    unittest.main()
