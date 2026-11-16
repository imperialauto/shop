"""Homepage queue: quick replies, sync after a reply anywhere, problem reports."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import quick_replies
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.imperial_task_model import PROBLEM_CATEGORY, ImperialTask
from app.main import app
from app.models import User
from app.routes import dispatch, imperial_tasks, internal_messaging

KEY = "test-internal-secret"
HEADERS = {"X-Internal-Key": KEY}


def item(**kw):
    base = dict(contact_name=None, vehicle_text=None, draft_reply=None)
    base.update(kw)
    return SimpleNamespace(**base)


class QuickReplyTests(unittest.TestCase):
    def labels(self, *args):
        return [s["label"] for s in quick_replies.suggestions(*args)]

    def test_unknown_lead_is_asked_name_and_vehicle_in_one_text(self):
        replies = quick_replies.suggestions(item())
        self.assertEqual(replies[0]["label"], "Ask name + vehicle")
        self.assertIn("first and last name", replies[0]["text"])
        self.assertIn("year, make and model", replies[0]["text"])

    def test_known_vehicle_is_never_asked_again(self):
        labels = self.labels(item(contact_name="Will Smith", vehicle_text="2014 Chevrolet Cruze"))
        self.assertNotIn("Ask vehicle", labels)
        self.assertNotIn("Ask name + vehicle", labels)

    def test_vin_is_asked_once_only(self):
        lead = item(contact_name="Will Smith", vehicle_text="2014 Chevrolet Cruze")
        self.assertIn("Ask VIN (once)", self.labels(lead, None, []))
        asked = [SimpleNamespace(body="Can you send the VIN when you get a chance?")]
        self.assertNotIn("Ask VIN (once)", self.labels(lead, None, asked))

    def test_never_more_than_four(self):
        self.assertLessEqual(len(quick_replies.suggestions(item())), 4)


class QueueSyncAndProblemTests(unittest.TestCase):
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
        for patcher in (patch.object(internal_messaging, "INTERNAL_API_KEY", KEY),
                        patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash="x", role="owner"))
            db.commit()

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def test_texting_a_customer_clears_every_open_item_for_them(self):
        old = datetime(2026, 11, 14, tzinfo=timezone.utc)
        with self.Session() as db:
            for item_id, conversation in ((1, "CNcall"), (2, "CNlead")):
                db.add(DispatchItem(id=item_id, quo_conversation_id=conversation, customer_phone="+15742217530",
                                    summary="head gasket", category="repair", urgency="normal",
                                    status="routine", last_message_id=f"m{item_id}", updated_at=old))
            db.add(DispatchItem(id=3, quo_conversation_id="CNother", customer_phone="+16025550100", summary="x",
                                category="repair", urgency="normal", status="routine", last_message_id="m3", updated_at=old))
            db.commit()
        response = self.client.post("/internal/dispatch/handled", headers=HEADERS, json={
            "conversationId": "CNnewtext", "at": "2026-11-15T16:40:00Z", "phone": "+15742217530"}).json()
        self.assertEqual(sorted(response["items"]), [1, 2])
        with self.Session() as db:
            self.assertEqual([db.get(DispatchItem, i).status for i in (1, 2, 3)], ["closed", "closed", "routine"])

    def test_last_message_from_the_shop_waits_on_them(self):
        updated = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        row = item(updated_at=updated)
        ours = SimpleNamespace(direction="outbound", created_at=updated + timedelta(hours=1))
        theirs = SimpleNamespace(direction="inbound", created_at=updated + timedelta(hours=1))
        self.assertTrue(dispatch._last_from_us(row, [ours]))
        self.assertFalse(dispatch._last_from_us(row, [theirs]))
        self.assertFalse(dispatch._last_from_us(row, []))

    def test_problem_report_lands_on_fix_list_not_todo(self):
        with patch("app.main.get_current_user", side_effect=lambda request, db: db.get(User, 1)):
            saved = self.client.post("/problems", data={"page": "/dashboard", "comment": "Task 118 won't open"},
                                     follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            self.assertEqual(saved.headers["location"], "/dashboard?reported=1")
            listing = self.client.get("/problems")
        self.assertIn("Task 118 won&#39;t open", listing.text)
        with self.Session() as db:
            task = db.query(ImperialTask).one()
            self.assertEqual(task.category, PROBLEM_CATEGORY)
            self.assertIn("Page: /dashboard", task.details)
            self.assertEqual(imperial_tasks.owner_dashboard_tasks(db, "owner")["tasks"], [])
        internal = self.client.get("/internal/imperial/tasks/problems", headers=HEADERS).json()
        self.assertEqual(len(internal["problems"]), 1)

    def test_problem_report_ignores_offsite_return_page(self):
        with patch("app.main.get_current_user", side_effect=lambda request, db: db.get(User, 1)):
            saved = self.client.post("/problems", data={"page": "//evil.example", "comment": "broken thing"},
                                     follow_redirects=False)
        self.assertEqual(saved.headers["location"], "/dashboard?reported=1")


if __name__ == "__main__":
    unittest.main()


class DraftRetryTests(unittest.TestCase):
    """Recovery D: customer messages without an AI draft are retried and the card says so."""

    setUp = QueueSyncAndProblemTests.setUp
    tearDown = QueueSyncAndProblemTests.tearDown

    def add(self, item_id, minutes_ago=10, draft=None, phone="+16025550100", status="needs_owner"):
        with self.Session() as db:
            db.add(DispatchItem(id=item_id, quo_conversation_id=f"CN{item_id}", customer_phone=phone, summary="brakes",
                                category="repair", urgency="normal", status=status, last_message_id=f"m{item_id}",
                                draft_reply=draft, updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)))
            db.commit()

    def needed(self):
        return [i["itemId"] for i in self.client.get("/internal/dispatch/drafts-needed", headers=HEADERS).json()["items"]]

    def fail(self, item_id, reason="empty draft"):
        return self.client.post("/internal/dispatch/draft-failed", headers=HEADERS,
                                json={"conversationId": f"CN{item_id}", "messageId": f"m{item_id}", "reason": reason}).json()

    def test_only_undrafted_settled_customer_messages_are_listed(self):
        self.add(1)                      # needs a draft
        self.add(2, draft="Hi there")    # already drafted
        self.add(3, minutes_ago=0)       # webhook may still be drafting
        self.add(4, draft="")            # no reply needed
        self.assertEqual(self.needed(), [1])
        self.assertEqual(self.client.get("/internal/dispatch/drafts-needed").status_code, 401)

    def test_failures_are_counted_per_message_and_stop_after_the_limit(self):
        self.add(1)
        self.assertEqual(self.fail(1)["attempts"], 1)
        self.fail(1)
        self.assertEqual(self.needed(), [1])
        self.assertEqual(self.fail(1)["attempts"], dispatch.DRAFT_RETRY_LIMIT)
        self.assertEqual(self.needed(), [])
        self.assertEqual(self.client.post("/internal/dispatch/draft-failed", headers=HEADERS, json={
            "conversationId": "CN1", "messageId": "older", "reason": "x"}).json(), {"recorded": False})

    def test_card_shows_retrying_then_gave_up_then_clears_when_a_draft_lands(self):
        self.add(1)
        with self.Session() as db:
            item = db.get(DispatchItem, 1)
            self.assertIsNone(dispatch._draft_state(db, item))
            self.fail(1)
            db.expire_all()
            self.assertEqual(dispatch._draft_state(db, item), "retrying")
            self.fail(1); self.fail(1)
            db.expire_all()
            self.assertEqual(dispatch._draft_state(db, item), "gave_up")
        saved = self.client.post("/internal/dispatch/draft", headers=HEADERS,
                                 json={"conversationId": "CN1", "messageId": "m1", "body": "Hey, what year is it?"}).json()
        self.assertTrue(saved["recorded"])
        with self.Session() as db:
            self.assertIsNone(dispatch._draft_state(db, db.get(DispatchItem, 1)))
