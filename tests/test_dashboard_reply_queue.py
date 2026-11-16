"""An approved dashboard reply must reach the send queue (Nov 16 production finding).

On PostgreSQL the dispatch_outbound status constraint didn't allow 'queued', so every
approved reply's insert failed and decide() swallowed the error. The model now mirrors
the constraint, so this SQLite test fails the same way production did if it regresses.
Synthetic data only; nothing is sent.
"""
import pathlib
import re
import unittest
from unittest.mock import patch
import fixture_environment  # noqa

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import User
from app.routes.dispatch import queued_replies


class DashboardReplyQueueTests(unittest.TestCase):
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
            db.add(User(id=1, username="owner", password_hash="x", role="owner"))
            db.add(DispatchItem(id=59, quo_conversation_id="CN59", customer_phone="+16235550159", status="needs_owner",
                                summary="TEST ONLY: can I come Monday?", last_message_id="AC59", draft_reply="Monday works."))
            db.commit()
        login = patch("app.routes.dispatch.get_current_user", side_effect=lambda request, db: db.get(User, 1))
        login.start()
        self.addCleanup(login.stop)
        self.client = TestClient(app)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def approve(self, text="Monday at 2 works."):
        return self.client.post("/dispatch/59/decide", data={"decision": "approve", "expected_message_id": "AC59",
                                                             "reply_text": text}, follow_redirects=False)

    def test_approved_reply_is_queued_and_item_closed(self):
        self.assertEqual(self.approve().status_code, 303)
        with self.Session() as db:
            self.assertEqual(db.get(DispatchItem, 59).status, "approved")
            self.assertEqual([r.status for r in db.query(DispatchOutbound).all()], ["queued"])
            replies = queued_replies(db)
        self.assertEqual([(r["phone"], r["text"]) for r in replies], [("+16235550159", "Monday at 2 works.")])

    def test_model_rejects_statuses_production_rejects(self):
        from sqlalchemy.exc import IntegrityError
        with self.Session() as db:
            db.add(DispatchOutbound(dedupe_key="x", kind="dashboard_reply", item_id=59, status="pending"))
            with self.assertRaises(IntegrityError):
                db.commit()

    def test_a_failed_insert_is_not_shown_as_sent(self):
        with patch("app.routes.dispatch.DispatchOutbound",
                   side_effect=lambda **kw: DispatchOutbound(**{**kw, "status": "bogus"})):
            response = self.approve()
        self.assertEqual(response.status_code, 503)
        self.assertIn("nothing was sent", response.text)
        with self.Session() as db:
            self.assertEqual(db.get(DispatchItem, 59).status, "needs_owner")  # still open, visibly
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_constraint_matches_the_migration(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        sql = (root / "migrations" / "20261116_dispatch_outbound_queued.sql").read_text()
        allowed = set(re.findall(r"'(\w+)'", sql.split("CHECK", 1)[1]))
        model = DispatchOutbound.__table__.constraints
        check = next(c for c in model if getattr(c, "name", "") == "dispatch_outbound_valid_status")
        self.assertEqual(allowed, set(re.findall(r"'(\w+)'", str(check.sqltext))))
        self.assertIn("queued", allowed)


if __name__ == "__main__":
    unittest.main()
