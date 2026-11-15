"""Texting a customer from the customer file or a task card. Synthetic SQLite; nothing is sent."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import fixture_environment  # noqa

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import task_board
from app.appointments import PHOENIX
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.imperial_task_model import ImperialTask
from app.main import app
from app.models import Customer, User
from app.routes.dispatch import back_to, queued_replies


class ComposeTests(unittest.TestCase):
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
                        User(id=2, username="tech", password_hash="x", role="tech"),
                        Customer(id=1, first_name="TEST", last_name="ONLY", phone="(623) 555-0100")])
            db.commit()
        self.user = 1
        login = patch("app.routes.dispatch.get_current_user", side_effect=lambda request, db: db.get(User, self.user))
        login.start()
        self.addCleanup(login.stop)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def compose(self, **form):
        data = {"phone": "(623) 555-0100", "reply_text": "Monday at 2 works. See you then.",
                "newest_message_id": "AC1", "next": "/customers/1#file", **form}
        return self.client.post("/dispatch/compose", data=data, follow_redirects=False)

    def test_new_text_is_queued_once_against_the_newest_message(self):
        response = self.compose()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/customers/1#file")
        with self.Session() as db:
            replies = queued_replies(db)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["messageId"], "AC1")
        self.assertEqual(replies[0]["phone"], "+16235550100")
        with self.Session() as db:
            self.assertEqual(queued_replies(db), [])  # handed over once

    def test_reply_answers_the_waiting_item(self):
        with self.Session() as db:
            db.add(DispatchItem(quo_conversation_id="CN1", customer_phone="+16235550100", status="needs_owner",
                                summary="Can I come Monday?", last_message_id="AC1", draft_reply="Sure"))
            db.commit()
        self.compose()
        with self.Session() as db:
            items = db.query(DispatchItem).all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].status, "approved")
            self.assertEqual(items[0].draft_reply, "Monday at 2 works. See you then.")

    def test_refuses_without_a_checked_message_or_for_techs(self):
        self.assertEqual(self.compose(newest_message_id="").status_code, 409)
        self.user = 2
        self.assertEqual(self.compose().status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_paused_texting_blocks(self):
        with patch("app.routes.dispatch.is_paused", return_value=True):
            self.assertEqual(self.compose().status_code, 409)

    def test_redirect_stays_on_site(self):
        self.assertEqual(back_to("https://evil.example"), "/dashboard")
        self.assertEqual(back_to("//evil.example"), "/dashboard")
        self.assertEqual(back_to("/dashboard/tasks?tab=now"), "/dashboard/tasks?tab=now")


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_one_card_per_conversation_in_the_right_tab(self):
        now = datetime(2026, 11, 14, 17, 0, tzinfo=PHOENIX)
        info = json.dumps({"conversationId": "CN1", "phone": "+16232049200"})
        with self.Session() as db:
            db.add(Customer(id=1, first_name="Anastasia", last_name="Carpenter", phone="623-204-9200"))
            db.add(DispatchItem(quo_conversation_id="CN1", customer_phone="+16232049200", customer_id=1,
                                vehicle_text="2003 Chevrolet Suburban", status="needs_owner", last_message_id="m"))
            for n, title in enumerate(["[REPLY DUE] Suburban after 4pm", "[REPLY DRAFT — UPDATE] Suburban Monday"]):
                db.add(ImperialTask(source="quo", external_id=f"TK{n}", title=title, details=info,
                                    created_at=now - timedelta(hours=5 - n)))
            db.add(ImperialTask(source="dashboard", external_id="d1", title="Call Snap Finance",
                                due_at=now + timedelta(days=2)))
            db.add(ImperialTask(source="dashboard", external_id="d2", title="Waiting on parts", status="blocked"))
            db.commit()
            data = task_board.board(db, now)
        self.assertEqual([t["count"] for t in data["tabs"]], [1, 1, 1, 0])
        card = data["cards"]["now"][0]
        self.assertEqual(card["name"], "Anastasia Carpenter")
        self.assertEqual(card["vehicle"], "2003 Chevrolet Suburban")
        self.assertEqual(card["headline"], "Suburban Monday")
        self.assertEqual(len(card["older"]), 1)
        self.assertIsNotNone(card["item"])
        self.assertEqual(data["cards"]["later"][0]["headline"], "Call Snap Finance")


if __name__ == "__main__":
    unittest.main()
