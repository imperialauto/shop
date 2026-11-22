"""Held automatic texts get tied to the customer's newest real text when the owner presses Send.
Made-up data, private database, no live services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import customer_file, dispatch_anchor
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import User
from app.routes.dispatch import queued_replies

MADE = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


def at(minutes):
    return (MADE + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def thread(*stamps):
    return lambda path, phone: ({"messages": [{"id": f"AC{i}", "createdAt": s} for i, s in enumerate(stamps)]}, None)


class DispatchAnchorTests(unittest.TestCase):
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
        self.client = TestClient(app)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash="x", role="owner"))
            db.add(DispatchItem(id=7, quo_conversation_id="LINK5", customer_phone="+16235550100", status="needs_owner",
                                category="deposit_request", summary="Deposit", draft_reply="Deposit please. Jae",
                                last_message_id="deposit:5:1763661600", thread_message_id=None, created_at=MADE))
            db.add(DispatchItem(id=8, quo_conversation_id="CN1", customer_phone="+16235550100", status="needs_owner",
                                summary="How much?", last_message_id="AC1", created_at=MADE))
            db.commit()
        login = patch("app.routes.dispatch.get_current_user", side_effect=lambda request, db: db.get(User, 1))
        login.start()
        self.addCleanup(login.stop)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def send(self, item_id=7, expected="deposit:5:1763661600", fetch=None):
        with patch.object(customer_file, "_bridge", fetch or thread(at(-60), at(-5))):
            return self.client.post(f"/dispatch/{item_id}/decide", data={
                "decision": "approve", "expected_message_id": expected,
                "reply_text": "Deposit please. Jae"}, follow_redirects=False)

    def test_send_ties_the_draft_to_the_newest_real_text(self):
        self.assertEqual(self.send().status_code, 303)
        with self.Session() as db:
            [reply] = queued_replies(db)
            self.assertEqual(reply["messageId"], "AC1")
            self.assertEqual(db.get(DispatchItem, 7).status, "approved")

    def test_unreadable_thread_queues_nothing(self):
        response = self.send(fetch=lambda path, phone: (None, "unavailable"))
        self.assertEqual(response.status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
            self.assertEqual(db.get(DispatchItem, 7).status, "needs_owner")

    def test_a_text_after_the_draft_blocks_the_send(self):
        response = self.send(fetch=thread(at(-60), at(10)))
        self.assertEqual(response.status_code, 409)
        self.assertIn("newer text", response.text)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_ordinary_customer_items_are_not_touched(self):
        def never(path, phone):
            raise AssertionError("no thread read for an ordinary reply")
        self.assertEqual(self.send(item_id=8, expected="AC1", fetch=never).status_code, 303)
        with self.Session() as db:
            [reply] = queued_replies(db)
            self.assertEqual(reply["messageId"], "AC1")

    def test_needs_anchor_only_for_made_up_ids(self):
        self.assertTrue(dispatch_anchor.needs_anchor(DispatchItem(last_message_id="pickup:3:1")))
        self.assertFalse(dispatch_anchor.needs_anchor(DispatchItem(last_message_id="pickup:3:1", thread_message_id="AC4")))
        self.assertFalse(dispatch_anchor.needs_anchor(DispatchItem(last_message_id="compose:abc")))


if __name__ == "__main__":
    unittest.main()
