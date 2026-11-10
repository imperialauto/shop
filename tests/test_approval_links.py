"""Approval links by text through the owner's YES. Made-up data, private database, no live services."""
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import approval_links, customer_file
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.routes import dispatch, internal_messaging

PASSWORD = "fixture-password-1"
SIGNATURE = "data:image/png;base64,iVBORw0KGgo="
KEY = "fixture-internal-key"
HEADERS = {"X-Internal-Key": KEY}
OWNER = "+16025550199"
AZ = ZoneInfo("America/Phoenix")


def thread(path, phone):
    assert (path, phone) == ("/conversation", "6025550100")
    return {"messages": [{"id": "AC1", "direction": "INBOUND"}, {"id": "AC2", "direction": "OUTBOUND"}]}, None


class ApprovalLinkTests(unittest.TestCase):
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
        for target, attr, value in ((internal_messaging, "INTERNAL_API_KEY", KEY), (dispatch, "OWNER_PHONE", OWNER)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"))
            db.add(Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"))
            db.add(RepairOrder(id=5, customer_id=1, vehicle_id=1, status="diagnosing", public_token="made-up-token"))
            db.add_all([LineItem(repair_order_id=5, description="Front pads", unit_price=349),
                        LineItem(repair_order_id=5, description="Flush", unit_price=99)])
            db.commit()
        self.owner = TestClient(app, follow_redirects=False)
        self.owner.post("/login", data={"username": "owner", "password": PASSWORD})
        self.internal = TestClient(app)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def items(self, prefix="LINK"):
        with self.Session() as db:
            return [i for i in db.query(DispatchItem).order_by(DispatchItem.id).all() if i.quo_conversation_id.startswith(prefix)]

    def ask(self, fetch=thread):
        with patch.object(customer_file, "_bridge", fetch):
            return self.owner.post("/ro/5/request-approval").headers["location"]

    def owner_text(self, ident, body):
        return self.internal.post("/internal/dispatch/intake", headers=HEADERS,
                                  json={"conversationId": "CNowner", "messageId": ident, "phone": OWNER, "body": body}).json()

    def alerts(self):
        with self.Session() as db:
            return dispatch.due_owner_messages(db, datetime(2026, 11, 9, 9, 0, tzinfo=AZ))

    def test_send_for_approval_queues_one_item_and_sends_nothing(self):
        location = self.ask()
        item = self.items()[0]
        self.assertIn(f"link=queued{item.id}", location)
        self.assertEqual((item.status, item.category, item.customer_phone, item.last_message_id),
                         ("needs_owner", "approval_link", "+16025550100", "AC2"))
        self.assertIn("Hi Pat, your estimate for the 2016 Ford Focus is ready", item.draft_reply)
        self.assertIn("/status/made-up-token", item.draft_reply)
        self.assertIn("2 repairs, $448.00", item.summary)
        self.ask()  # a second tap refreshes the same item
        self.assertEqual(len(self.items()), 1)
        page = self.owner.get(location)
        self.assertIn(f"Reply YES {item.id}", page.text)
        self.assertIn("Nothing has been sent yet", page.text)

    def test_owner_yes_hands_the_link_to_the_messaging_service_once(self):
        self.ask()
        item = self.items()[0]
        due = [m for m in self.alerts() if m["key"].startswith(f"alert:{item.id}:")]
        self.assertEqual(len(due), 1)
        self.assertIn(f"Approval link for", due[0]["text"])
        self.assertIn(f"YES {item.id} texts it to them", due[0]["text"])
        self.internal.post("/internal/dispatch/outbound", headers=HEADERS, json={"key": due[0]["key"], "status": "sent"})
        reply = self.owner_text("o1", f"YES {item.id}")
        sent = reply["customerReply"]
        self.assertEqual((sent["phone"], sent["messageId"]), ("+16025550100", "AC2"))
        self.assertIn("/status/made-up-token", sent["text"])
        self.assertNotIn("customerReply", self.owner_text("o2", f"YES {item.id}"))

    def test_stop_all_blocks_the_send(self):
        self.ask()
        item = self.items()[0]
        due = [m for m in self.alerts() if m["key"].startswith(f"alert:{item.id}:")][0]
        self.internal.post("/internal/dispatch/outbound", headers=HEADERS, json={"key": due["key"], "status": "sent"})
        self.owner_text("o0", "STOP ALL")
        reply = self.owner_text("o1", f"YES {item.id}")
        self.assertNotIn("customerReply", reply)
        self.assertIn("stopped", reply["ownerReply"])

    def test_no_phone_or_quo_down_queues_nothing(self):
        with self.Session() as db:
            db.get(Customer, 1).phone = None
            db.commit()
        self.assertIn("link=no_phone", self.ask())
        with self.Session() as db:
            db.get(Customer, 1).phone = "602-555-0100"
            db.commit()
        self.assertIn("link=unavailable", self.ask(lambda path, phone: (None, "unavailable")))
        self.assertEqual(self.items(), [])
        with self.Session() as db:
            self.assertEqual({li.approval for li in db.query(LineItem).all()}, {"pending"})

    def test_empty_thread_uses_none(self):
        self.ask(lambda path, phone: ({"messages": []}, None))
        self.assertEqual(self.items()[0].last_message_id, "none")

    def test_customer_answer_closes_the_link_and_a_second_round_does_not_collide(self):
        self.ask()
        customer = TestClient(app, follow_redirects=False)
        with self.Session() as db:
            ids = [li.id for li in db.query(LineItem).order_by(LineItem.id).all()]
        form = {"signature_data": SIGNATURE, "approved_by": "Pat", f"line_{ids[0]}": "approved", f"line_{ids[1]}": "declined"}
        self.assertEqual(customer.post("/sign/ro/made-up-token", data=form).status_code, 200)
        self.assertEqual(self.items()[0].status, "closed")
        self.assertEqual(len(self.items("WEB5-decision")), 1)
        # Second round on the same job while the first decision is still open.
        with self.Session() as db:
            db.add(LineItem(repair_order_id=5, description="Rotors", unit_price=200))
            db.commit()
            new_id = db.query(LineItem).filter(LineItem.description == "Rotors").one().id
        self.ask()
        self.assertEqual(customer.post("/sign/ro/made-up-token", data={"signature_data": SIGNATURE, f"line_{new_id}": "approved"}).status_code, 200)
        decisions = self.items("WEB5-decision")
        self.assertEqual(len(decisions), 1)
        self.assertIn("Approved 1 of 1", decisions[0].summary)

    def test_new_job_from_customer_page_lists_their_vehicle(self):
        page = self.owner.get("/ro/new?customer_id=1").text
        self.assertIn('<option value="1" selected>2016 Ford Focus', page)
        self.assertNotIn("Select customer first", page)
        with self.Session() as db:
            db.add(Customer(id=2, first_name="No", last_name="Car", phone="6025550111"))
            db.commit()
        self.assertIn("No vehicles on file", self.owner.get("/ro/new?customer_id=2").text)

    def test_vehicle_year_placeholder_uses_neutral_greeting(self):
        with self.Session() as db:
            db.get(Customer, 1).first_name = "2013 Ford fusion"
            db.commit()
        self.ask()
        item = self.items()[0]
        self.assertIn("Hi there, your estimate for the 2016 Ford Focus", item.draft_reply)
        self.assertIn("Review the itemized charges and any applicable tax", item.draft_reply)
        self.assertIn("verify tax treatment", item.summary)
        sign_page = TestClient(app).get("/sign/ro/made-up-token").text
        self.assertIn("Review the itemized charges and any applicable taxes", sign_page)
        self.assertNotIn("Before tax. Declined items", sign_page)

    def test_mobile_numbers(self):
        self.assertEqual(approval_links.mobile("1 (602) 555-0100"), "+16025550100")
        self.assertIsNone(approval_links.mobile("555-0100"))


if __name__ == "__main__":
    unittest.main()
