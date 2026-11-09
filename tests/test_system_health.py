"""Home health strip: quiet when fine, specific when not. Made-up data."""
import unittest
from datetime import datetime, timedelta, timezone

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import system_health
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchSync
from app.main import app
from app.models import Customer, User

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)
PASSWORD = "fixture-password-1"


class SystemHealthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([DispatchSync(source="quo_recovery", last_success_at=NOW - timedelta(minutes=4), consecutive_failures=0),
                        DispatchSync(source="backup", last_success_at=NOW - timedelta(hours=4), consecutive_failures=0)])
            db.commit()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def report(self):
        with self.Session() as db:
            return system_health.report(db, NOW)

    def test_all_good(self):
        r = self.report()
        self.assertTrue(r["ok"])
        self.assertEqual([c["name"] for c in r["checks"]], ["Quo texts and calls", "Nightly backup", "AI reply drafts"])

    def test_each_failure_is_named_with_what_to_do(self):
        with self.Session() as db:
            db.get(DispatchSync, "quo_recovery").last_success_at = NOW - timedelta(hours=1)
            db.get(DispatchSync, "backup").consecutive_failures = 1
            db.add(DispatchItem(quo_conversation_id="CN1", customer_phone="+16025550100", category="general", summary="x",
                                last_message_id="m1", status="needs_owner", draft_reply=None, updated_at=NOW - timedelta(minutes=30)))
            db.add(Customer(first_name="Unknown", last_name="(0100)", phone="+16025550100"))
            db.commit()
        r = self.report()
        self.assertFalse(r["ok"])
        self.assertEqual([p["name"] for p in r["problems"]], ["Quo texts and calls", "Nightly backup", "AI reply drafts"])
        self.assertTrue(all(p["fix"] for p in r["problems"]))
        self.assertIn("1 without a name, 1 still to look up in Quo.", r["checks"][-1]["detail"])

    def test_owner_sees_it_on_home(self):
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.get(DispatchSync, "backup").consecutive_failures = 2
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        client = TestClient(app)
        client.post("/login", data={"username": "owner", "password": PASSWORD})
        page = client.get("/dashboard").text
        self.assertIn('id="system-health"', page)
        self.assertIn("Nightly backup", page)


if __name__ == "__main__":
    unittest.main()
