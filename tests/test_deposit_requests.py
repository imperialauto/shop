"""Deposit text after a link approval. Made-up data, private database, no live services."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import customer_file, deposit_requests
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, Vehicle
from app.routes import dispatch

SIGNATURE = "data:image/png;base64,iVBORw0KGgo="


def thread(path, phone):
    assert (path, phone) == ("/conversation", "6025550100")
    return {"messages": [{"id": "AC1", "direction": "INBOUND"}, {"id": "AC9", "direction": "OUTBOUND"}]}, None


class DepositTextTests(unittest.TestCase):
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
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"))
            db.add(Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"))
            db.add(RepairOrder(id=5, customer_id=1, vehicle_id=1, status="waiting_approval", public_token="made-up-token"))
            db.add_all([LineItem(repair_order_id=5, description="Clutch labor", item_type="labor", unit_price=349, approval="pending"),
                        LineItem(repair_order_id=5, description="Clutch kit", item_type="part", unit_price=100, approval="pending")])
            db.commit()
            self.lines = [li.id for li in db.query(LineItem).order_by(LineItem.id).all()]
        self.customer = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign(self, *choices, on=True, fetch=thread):
        form = {"signature_data": SIGNATURE, "approved_by": "Pat"}
        form.update({f"line_{i}": c for i, c in zip(self.lines, choices)})
        with patch.object(deposit_requests, "enabled", lambda: on), patch.object(customer_file, "_bridge", fetch):
            self.assertEqual(self.customer.post("/sign/ro/made-up-token", data=form).status_code, 200)

    def deposits(self):
        with self.Session() as db:
            return [(i.status, i.draft_reply, i.thread_message_id)
                    for i in db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == "DEP5").all()]

    def test_off_by_default_sends_nothing(self):
        self.sign("approved", "approved", on=False)
        self.assertEqual(self.deposits(), [])
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_setting_is_off_unless_saved(self):
        with patch.dict("os.environ", {}, clear=False):
            self.assertFalse(deposit_requests.enabled())

    def test_approval_queues_one_half_deposit_text_for_the_messaging_service(self):
        self.sign("approved", "approved")
        [(status, text, newest)] = self.deposits()
        self.assertEqual((status, newest), ("approved", "AC9"))
        # $449 + 9.2% tax on the $100 part = $458.20, half is $229.10
        self.assertIn("Hi Pat, thanks for approving the work on your 2016 Ford Focus.", text)
        self.assertIn("$229.10 deposit, half of the $458.20 total, by Zelle to jaelan@imperialautocarellc.com or cash", text)
        self.assertIn("Snap Finance", text)
        self.assertIn("Just ask and I'll send the link", text)
        self.assertNotIn("http", text)  # no Snap link unless they ask
        self.assertTrue(text.endswith("Jae"))
        with self.Session() as db:
            replies = dispatch.queued_replies(db)
            self.assertEqual(len(replies), 1)
            self.assertEqual((replies[0]["phone"], replies[0]["messageId"]), ("+16025550100", "AC9"))
            self.assertEqual(dispatch.queued_replies(db), [])  # handed over once, never retried

    def test_only_approved_lines_count(self):
        self.sign("approved", "declined")
        [(_, text, _)] = self.deposits()
        self.assertIn("$174.50 deposit, half of the $349.00 total", text)

    def test_all_declined_sends_nothing(self):
        self.sign("declined", "declined")
        self.assertEqual(self.deposits(), [])

    def test_unreadable_thread_waits_for_the_owner(self):
        self.sign("approved", "approved", fetch=lambda path, phone: (None, "unavailable"))
        [(status, text, _)] = self.deposits()
        self.assertEqual(status, "needs_owner")
        self.assertIn("$229.10", text)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_money_already_in_sends_nothing(self):
        with self.Session() as db:
            inv = Invoice(repair_order_id=5, total=458.2, status="sent")
            db.add(inv)
            db.flush()
            db.add(Payment(invoice_id=inv.id, amount=200, method="zelle"))
            db.commit()
        self.sign("approved", "approved")
        self.assertEqual(self.deposits(), [])

    def test_a_failure_never_blocks_the_approval(self):
        def broken(path, phone):
            raise RuntimeError("bridge exploded")
        self.sign("approved", "approved", fetch=broken)
        self.assertEqual(self.deposits(), [])
        with self.Session() as db:
            self.assertEqual({li.approval for li in db.query(LineItem).all()}, {"approved"})
            self.assertEqual(db.get(RepairOrder, 5).status, "approval_received")


if __name__ == "__main__":
    unittest.main()
