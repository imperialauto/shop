"""Customer file timeline, on made-up records in a private database. No live services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import customer_file
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Communication, Customer, Invoice, LineItem, Payment, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
T0 = datetime(2026, 11, 1, 17, 0)


def live(path, phone):
    assert phone == "6025550100"
    if path == "/conversation":
        return {"messages": [
            {"id": "MSG1", "direction": "INBOUND", "body": "Brakes grinding", "createdAt": "2026-11-02T16:00:00Z", "mediaUrls": ["https://example.test/p.jpg"]},
            {"id": "MSG2", "direction": "OUTBOUND", "body": "Bring it Tuesday", "createdAt": "2026-11-02T16:05:00Z", "mediaUrls": []},
        ]}, None
    return {"calls": [
        {"id": "C1", "direction": "INBOUND", "duration": 0, "createdAt": "2026-11-03T15:00:00Z", "voicemail": "Call me back"},
        {"id": "C2", "direction": "OUTBOUND", "duration": 300, "createdAt": "2026-11-03T16:00:00Z", "summary": "Approved pads."},
    ]}, None


def down(path, phone):
    return None, "unavailable"


class CustomerFileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"),
                        User(id=2, username="tech", password_hash=hash_password(PASSWORD), role="tech")])
            pat = Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100")
            other = Customer(id=2, first_name="Lee", last_name="Other", phone="+14805559999")
            db.add_all([pat, other])
            db.flush()
            v = Vehicle(customer_id=1, year=2016, make="Ford", model="Focus")
            db.add(v)
            db.flush()
            ro = RepairOrder(id=7, customer_id=1, vehicle_id=v.id, status="waiting_approval", concern="Brakes", created_at=T0)
            db.add(ro)
            db.flush()
            db.add_all([
                LineItem(repair_order_id=7, description="Front pads", unit_price=349, approval="approved", decided_at=T0 + timedelta(days=2)),
                LineItem(repair_order_id=7, description="Rotors", unit_price=200, approval="declined", decided_at=T0 + timedelta(days=2)),
                LineItem(repair_order_id=7, description="Flush", unit_price=99, approval="pending", created_at=T0 + timedelta(days=1)),
            ])
            inv = Invoice(id=3, repair_order_id=7, total=380.0, status="sent", created_at=T0 + timedelta(days=3))
            db.add(inv)
            db.flush()
            db.add(Payment(invoice_id=3, amount=100.0, method="card", paid_at=T0 + timedelta(days=4)))
            # A saved copy of MSG1 (must not show twice) and an older text the live lookup no longer returns.
            db.add_all([Communication(customer_id=1, direction="inbound", body="Brakes grinding", external_id="MSG1"),
                        Communication(customer_id=1, direction="inbound", body="Old text", external_id="OLD", created_at=T0 - timedelta(days=30))])
            # Came in before the customer record existed: matched by phone only.
            db.add(DispatchItem(quo_conversation_id="CN1", customer_phone="+16025550100", category="estimate",
                                summary="Asked about brakes", last_message_id="MSG1", created_at=datetime(2026, 10, 31, tzinfo=timezone.utc)))
            # Someone else's item must never appear.
            db.add(DispatchItem(quo_conversation_id="CN2", customer_phone="+14805559999", category="general",
                                summary="Not Pat", last_message_id="X", created_at=datetime(2026, 10, 31, tzinfo=timezone.utc)))
            db.commit()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)

    def build(self, fetch):
        with self.Session() as db:
            return customer_file.build(db, db.get(Customer, 1), fetch=fetch)

    def test_everything_lands_in_one_file_newest_first(self):
        f = self.build(live)
        titles = [e["title"] for e in f["entries"]]
        self.assertEqual(f["problems"], {})
        self.assertEqual(sum(1 for e in f["entries"] if e["detail"] == "Brakes grinding"), 1)
        self.assertIn("Old text", [e["detail"] for e in f["entries"]])
        self.assertIn("Missed call", titles)
        self.assertIn("Shop called · 5 min", titles)
        self.assertIn("Customer approved a repair on job #7", titles)
        self.assertIn("Customer declined a repair on job #7", titles)
        self.assertIn("Waiting on customer approval for job #7", titles)
        self.assertIn("Queue item #1 · estimate", titles)
        self.assertNotIn("Not Pat", [e["detail"] for e in f["entries"]])
        self.assertEqual([e["at"] for e in f["entries"]], sorted((e["at"] for e in f["entries"]), reverse=True))
        self.assertEqual(f["money"], {"billed": 380.0, "paid": 100.0, "balance": 280.0})
        self.assertEqual(f["counts"]["call"], 2)
        self.assertEqual(f["entries"][[e["detail"] for e in f["entries"]].index("Brakes grinding")]["media"], ["https://example.test/p.jpg"])

    def test_messaging_outage_is_said_not_hidden(self):
        f = self.build(down)
        self.assertEqual(f["problems"], {"text": "unavailable", "call": "unavailable"})
        self.assertIn("Brakes grinding", [e["detail"] for e in f["entries"]])  # saved copy still shown

    def test_no_phone_is_explained(self):
        with self.Session() as db:
            db.get(Customer, 1).phone = None
            db.commit()
        calls = []
        f = self.build(lambda *a: calls.append(a))
        self.assertEqual(calls, [])
        self.assertEqual(f["problems"], {"text": "no_phone", "call": "no_phone"})

    def test_bridge_is_off_until_configured(self):
        with patch.dict("os.environ", {"MESSAGING_BRIDGE_URL": "", "OPERATIONS_BRIDGE_TOKEN": ""}):
            self.assertEqual(customer_file._bridge("/calls", "6025550100"), (None, "not_connected"))

    def test_page_renders_filters_and_blocks_tech(self):
        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        with patch.object(customer_file, "_bridge", live):
            client = TestClient(app)
            client.post("/login", data={"username": "owner", "password": PASSWORD})
            page = client.get("/customers/1")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Customer file", page.text)
            self.assertIn("Owes <strong", page.text)
            self.assertIn("$280.00", page.text)
            calls_only = client.get("/customers/1?show=call")
            self.assertIn("Approved pads.", calls_only.text)
            self.assertNotIn("Bring it Tuesday", calls_only.text)
            self.assertEqual(client.get("/customers/1?show=bogus").status_code, 200)
            tech = TestClient(app)
            tech.post("/login", data={"username": "tech", "password": PASSWORD})
            self.assertNotEqual(tech.get("/customers/1", follow_redirects=False).status_code, 200)


if __name__ == "__main__":
    unittest.main()
