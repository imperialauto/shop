"""The "Offer Snap Finance" checkbox on customer texts. Synthetic SQLite; nothing is sent."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import snap_offer
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, User

LINE = snap_offer.LINE


class SnapLineTests(unittest.TestCase):
    def test_left_off_unless_checked(self):
        self.assertEqual(snap_offer.add("Your total is $900. Jae", False), "Your total is $900. Jae")

    def test_goes_before_the_sign_off(self):
        self.assertEqual(snap_offer.add("Your total is $900. Jae", True), f"Your total is $900. {LINE} Jae")
        self.assertEqual(snap_offer.add("Questions? - Imperial Auto Care", True), f"Questions? {LINE} - Imperial Auto Care")
        self.assertEqual(snap_offer.add("Your total is $900.", True), f"Your total is $900. {LINE}")

    def test_added_once_and_never_with_a_link(self):
        once = snap_offer.add("Total $900. Jae", True)
        self.assertEqual(snap_offer.add(once, True), once)
        self.assertEqual(snap_offer.add("I can send the Snap link. Jae", True), "I can send the Snap link. Jae")
        self.assertNotIn("http", once)

    def test_empty_text_stays_empty(self):
        self.assertEqual(snap_offer.add("", True), "")

    def test_checkbox_values(self):
        self.assertTrue(snap_offer.checked("1"))
        self.assertTrue(snap_offer.checked("on"))
        self.assertFalse(snap_offer.checked(""))
        self.assertFalse(snap_offer.checked(None))


class SnapCheckboxTests(unittest.TestCase):
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
            db.add_all([User(id=1, username="owner", password_hash="x", role="owner"),
                        Customer(id=1, first_name="TEST", last_name="ONLY", phone="(623) 555-0100")])
            db.commit()
        login = patch("app.routes.dispatch.get_current_user", side_effect=lambda request, db: db.get(User, 1))
        login.start()
        self.addCleanup(login.stop)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sent(self):
        with self.Session() as db:
            return [i.draft_reply for i in db.query(DispatchItem).filter(DispatchItem.status == "approved")]

    def compose(self, **form):
        data = {"phone": "(623) 555-0100", "reply_text": "The brakes come to $820. Jae",
                "newest_message_id": "AC1", **form}
        return self.client.post("/dispatch/compose", data=data, follow_redirects=False)

    def test_compose_adds_the_line_only_when_checked(self):
        self.assertEqual(self.compose().status_code, 303)
        self.assertEqual(self.sent(), ["The brakes come to $820. Jae"])

    def test_compose_with_snap_checked(self):
        self.assertEqual(self.compose(offer_snap="1").status_code, 303)
        self.assertEqual(self.sent(), [f"The brakes come to $820. {LINE} Jae"])

    def test_reply_to_a_waiting_item_with_snap_checked(self):
        with self.Session() as db:
            db.add(DispatchItem(id=7, quo_conversation_id="CN1", customer_phone="+16235550100", status="needs_owner",
                                summary="How much?", last_message_id="AC1"))
            db.commit()
        response = self.client.post("/dispatch/7/decide", data={
            "decision": "approve", "expected_message_id": "AC1", "offer_snap": "1",
            "reply_text": "It comes to $820. Jae"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.sent(), [f"It comes to $820. {LINE} Jae"])


if __name__ == "__main__":
    unittest.main()
