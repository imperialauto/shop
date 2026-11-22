"""A customer's plain yes text to a quote moves their one waiting job to Approved.
Made-up data, private database. Nothing here texts anyone."""
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import text_approval
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.imperial_task_model import ImperialTask
from app.main import app
from app.models import Communication, Customer, LineItem, RepairOrder, User, Vehicle
from app.routes import internal_messaging

MIRROR = "/internal/messaging/communications"
KEY = "fixture-internal-key"
PHONE = "+16025550100"


class PlainYesTests(unittest.TestCase):
    def test_yes_words(self):
        for body in ("Yes", "yes!", "Yes please", "ok go ahead", "Let's do it", "Sounds good", "approved 👍", "Yep"):
            self.assertTrue(text_approval.is_plain_yes(body), body)

    def test_not_a_yes(self):
        for body in ("Yes but can you do it cheaper", "yes?", "No", "not yet", "ok wait", "How much for brakes",
                     "Yes I think so but let me check with my wife first and get back to you", "", "Thanks",
                     "yes tomorrow", "Can't right now"):
            self.assertFalse(text_approval.is_plain_yes(body), body)


class TextYesTests(unittest.TestCase):
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
        for p in (patch.object(internal_messaging, "INTERNAL_API_KEY", KEY),
                  patch.dict(os.environ, {"TEXT_YES_APPROVES": "true", "IMPERIAL_TASKS_ENABLED": "true"})):
            p.start()
            self.addCleanup(p.stop)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash="x", role="owner"))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone=PHONE))
            db.add(Vehicle(id=1, customer_id=1, year=2015, make="Ford", model="Edge"))
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="waiting_approval"))
            db.add(LineItem(id=1, repair_order_id=1, description="Water pump", item_type="labor", quantity=1,
                            unit_price=500, approval="pending"))
            db.add(LineItem(id=2, repair_order_id=1, description="Coolant", item_type="part", quantity=1,
                            unit_price=40, approval="pending"))
            db.commit()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only
        self.engine.dispose()

    def quote(self, body="Your total is $540.00 with a 50% deposit. Want us to go ahead?", days_ago=1):
        with self.Session() as db:
            db.add(Communication(customer_id=1, direction="outbound", body=body,
                                 created_at=datetime.utcnow() - timedelta(days=days_ago)))
            db.commit()

    def text(self, body, ext="quo-in-1"):
        r = self.client.post(MIRROR, json={"phone": PHONE, "direction": "inbound", "body": body, "externalId": ext},
                             headers={"X-Internal-Key": KEY})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def lines(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, 1)
            return ro.status, [(li.approval, li.approval_source) for li in ro.line_items]

    def tasks(self):
        with self.Session() as db:
            return [t.title for t in db.query(ImperialTask).all()]

    def test_yes_after_quote_approves_the_job(self):
        self.quote()
        result = self.text("Yes please")
        self.assertEqual(result["textApproval"], {"action": "approved", "job": 1, "lines": 2})
        self.assertEqual(self.lines(), ("approval_received", [("approved", "text"), ("approved", "text")]))
        self.assertEqual(self.tasks(), ["Pat texted yes: job #1 moved to Approved"])

    def test_same_text_twice_does_nothing_more(self):
        self.quote()
        self.text("Yes")
        again = self.text("Yes")
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(len(self.tasks()), 1)

    def test_off_unless_switched_on(self):
        self.quote()
        with patch.dict(os.environ, {"TEXT_YES_APPROVES": ""}):
            result = self.text("Yes")
        self.assertNotIn("textApproval", result)
        self.assertEqual(self.lines()[0], "waiting_approval")

    def test_no_quote_no_change(self):
        self.quote(body="We can see you Monday at 2:30, does that work?")
        self.assertNotIn("textApproval", self.text("Yes"))
        self.assertEqual(self.lines()[0], "waiting_approval")

    def test_yes_to_deposit_pickup_or_reminder_texts_changes_nothing(self):
        # The shop's own non-quote texts name prices too; an "ok" to them is not a yes to new work.
        for body in (
            "Hi Pat, thanks for approving the work on your 2015 Ford Edge. To order parts and get you on the "
            "schedule, I'll need a $270.00 deposit, half of the $540.00 total, by Zelle to shop or cash. Jae",
            "Hi Pat, your 2015 Ford Edge is done and ready for pickup. The balance due is $270.00, by Zelle to "
            "shop or cash. Jae",
            "Hi Pat, just a reminder that the $75 from the diagnostic on your 2015 Ford Edge still goes toward "
            "the repair if you'd like us to take care of it. Just reply here and I'll get you on the schedule. Jae",
        ):
            with self.subTest(body=body[:30]):
                self.quote(body=body, days_ago=0)
                self.assertNotIn("textApproval", self.text("Ok", ext=f"quo-{hash(body)}"))
                self.assertEqual(self.lines(), ("waiting_approval", [("pending", None), ("pending", None)]))

    def test_yes_to_estimate_link_or_follow_up_approves(self):
        from app import text_approval
        self.assertTrue(text_approval.is_quote(
            "Hi Pat, your estimate for the 2015 Ford Edge is ready. Open your vehicle status and tap Review & Approve"))
        self.assertTrue(text_approval.is_quote(
            "Hi Pat, I wanted to check back on the water pump we quoted for your 2015 Ford Edge."))
        self.assertTrue(text_approval.is_quote("Brakes all around come to $480. 50% deposit to order parts."))
        self.assertFalse(text_approval.is_quote("Here's the link to apply for Snap Finance: x Jae"))

    def test_old_quote_no_change(self):
        self.quote(days_ago=10)
        self.assertNotIn("textApproval", self.text("Yes"))

    def test_conditional_yes_no_change(self):
        self.quote()
        self.assertNotIn("textApproval", self.text("Yes but only the water pump"))
        self.assertEqual(self.lines()[0], "waiting_approval")

    def test_two_open_jobs_asks_owner_and_changes_nothing(self):
        with self.Session() as db:
            db.add(RepairOrder(id=2, customer_id=1, vehicle_id=1, status="waiting_approval"))
            db.add(LineItem(id=3, repair_order_id=2, description="Brakes", item_type="labor", quantity=1,
                            unit_price=200, approval="pending"))
            db.commit()
        self.quote()
        result = self.text("Yes")
        self.assertEqual(result["textApproval"]["action"], "unclear")
        self.assertEqual(self.lines()[0], "waiting_approval")
        self.assertIn("pick which job", self.tasks()[0])

    def test_closes_queued_link_text(self):
        from app import approval_links
        with self.Session() as db:
            item = approval_links.queue(db, db.get(RepairOrder, 1), "msg-1", "owner")
            db.commit()
            item_id = item.id
        self.quote()
        self.text("Yes")
        with self.Session() as db:
            self.assertEqual(db.get(DispatchItem, item_id).status, "closed")

    def test_outbound_text_never_approves(self):
        r = self.client.post(MIRROR, json={"phone": PHONE, "direction": "outbound", "body": "Yes", "externalId": "o1"},
                             headers={"X-Internal-Key": KEY})
        self.assertNotIn("textApproval", r.json())

    def test_job_page_labels_it(self):
        self.quote()
        self.text("Yes")
        with open("app/templates/repair_orders/detail.html") as f:
            self.assertIn("Approved by text", f.read())


if __name__ == "__main__":
    unittest.main()
