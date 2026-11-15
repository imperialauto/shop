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
