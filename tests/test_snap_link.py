"""Snap Finance link when a customer clearly asks. Made-up data, private database, nothing is sent."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import snap_link
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Customer
from app.routes import dispatch, internal_messaging

KEY = "fixture-internal-key"
HEADERS = {"X-Internal-Key": KEY}
INTAKE = "/internal/dispatch/intake"


def message(ident, body, conversation="CNsnap"):
    return {"conversationId": conversation, "messageId": ident, "phone": "+16025550100", "body": body}


class ClearAskTests(unittest.TestCase):
    def test_clear_asks(self):
        for body in ("Can you send the Snap link?", "snap please", "Can I finance it?", "Do you guys offer financing?",
                     "send me the financing link", "I'd like to do financing", "Can I make payments on it"):
            self.assertTrue(snap_link.clear_ask(body), body)

    def test_not_clear_or_saying_no(self):
        for body in ("I don't need financing, I'll pay cash", "No snap thanks", "My snap got denied",
                     "When can I bring it in?", "Thanks", "x" * 170 + " snap"):
            self.assertFalse(snap_link.clear_ask(body), body)


class SnapLinkIntakeTests(unittest.TestCase):
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
                  patch.object(snap_link, "enabled", lambda: True)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app)
        with self.Session() as db:
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="602-555-0100"))
            db.commit()

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def post(self, ident, body, conversation="CNsnap"):
        return self.client.post(INTAKE, json=message(ident, body, conversation), headers=HEADERS).json()

    def replies(self):
        with self.Session() as db:
            return dispatch.queued_replies(db)

    def test_clear_ask_queues_the_link_once(self):
        response = self.post("AC1", "Can you send me the Snap link?")
        self.assertEqual(response["status"], "approved")
        [reply] = self.replies()
        self.assertEqual(reply["messageId"], "AC1")
        self.assertIn("https://snapf.in/IvkSFk7", reply["text"])
        self.assertTrue(reply["text"].endswith("Jae"))
        self.post("AC2", "snap please", "CNsnap2")  # same customer, same week
        self.assertEqual(self.replies(), [])

    def test_later_ai_draft_cannot_change_the_link_text(self):
        self.post("AC1", "Can you send me the Snap link?")
        for body in ("Something else entirely", dispatch.NO_REPLY):
            saved = self.client.post("/internal/dispatch/draft", headers=HEADERS,
                                     json={"conversationId": "CNsnap", "messageId": "AC1", "body": body}).json()
            self.assertFalse(saved["recorded"])
        [reply] = self.replies()
        self.assertIn("https://snapf.in/IvkSFk7", reply["text"])

    def test_off_by_default(self):
        patch.stopall()
        self.assertFalse(snap_link.enabled())

    def test_off_sends_nothing(self):
        with patch.object(snap_link, "enabled", lambda: False):
            self.assertNotEqual(self.post("AC1", "Can I finance it?")["status"], "approved")
        self.assertEqual(self.replies(), [])

    def test_ask_with_a_price_question_goes_to_the_owner(self):
        response = self.post("AC1", "Can I finance it? How much is it total?")
        self.assertEqual(response["status"], "needs_owner")
        self.assertEqual(self.replies(), [])

    def test_earlier_message_waiting_on_the_owner_keeps_it_with_the_owner(self):
        self.post("AC1", "How much for front brakes on my 2016 Ford Focus?")
        self.assertEqual(self.post("AC2", "Can I finance it?")["status"], "needs_owner")
        self.assertEqual(self.replies(), [])

    def test_unclear_mention_stays_with_the_owner(self):
        self.assertEqual(self.post("AC1", "I don't need financing, I'll pay cash")["status"], "needs_owner")
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_replayed_message_never_sends(self):
        body = {**message("AC1", "snap please"), "recovery": True}
        self.client.post(INTAKE, json=body, headers=HEADERS)
        self.assertEqual(self.replies(), [])


if __name__ == "__main__":
    unittest.main()
