"""Alert Center shows truthful Quo send state and separates recipients."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchOutbound
from app.main import app
from app.models import User
from app.routes import alert_center

PASSWORD = "fixture-password-1"


class AlertHistoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add_all([
                DispatchOutbound(dedupe_key="reply:1:m1", kind="customer", status="sent"),
                DispatchOutbound(dedupe_key="alert:1:m1", kind="owner_nudge", status="failed"),
                DispatchOutbound(dedupe_key="dashboard:2:m2", kind="dashboard_reply", status="skipped"),
            ])
            db.commit()
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"username": "owner", "password": PASSWORD})

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def test_customer_and_owner_history_accurately_labeled(self):
        page = self.client.get("/alerts")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Messaging activity", page.text)
        self.assertIn("To customer", page.text)
        self.assertIn("To owner", page.text)
        self.assertIn("Sent via Quo (delivery unconfirmed)", page.text)
        self.assertIn("Failed — review needed", page.text)
        self.assertIn("Not sent", page.text)
        self.assertNotIn(">Delivered<", page.text)
        self.assertNotIn("Failed, will retry", page.text)

    def test_outbound_kind_recipient_is_not_guessed_from_status(self):
        self.assertEqual(alert_center.recipient_for("customer"), "Customer")
        self.assertEqual(alert_center.recipient_for("dashboard_reply"), "Customer")
        self.assertEqual(alert_center.recipient_for("owner_alert"), "Owner")
        self.assertEqual(alert_center.STATUS["reserved"], "Send unconfirmed")


if __name__ == "__main__":
    unittest.main()
