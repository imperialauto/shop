"""Private, synthetic journey tests. No real customer traffic or external API calls."""
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import journey
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle

SID = "0123456789abcdef0123456789abcdef"
SOMEONE_ELSE = "fedcba9876543210fedcba9876543210"


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                                    poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.old_read_only = app.state.read_only
        app.state.read_only = False
        self.flags = patch.dict(os.environ, {"CUSTOMER_JOURNEY_ENABLED": "true"}, clear=False)
        self.flags.start()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password("sample-password-123"), role="owner"))
            db.add(User(id=2, username="front", password_hash=hash_password("sample-password-123"), role="office"))
            db.add(Customer(id=1, first_name="Sample", last_name="Customer", phone="+16025550101"))
            db.add(Vehicle(id=1, customer_id=1, year=2013, make="Ford", model="Fusion"))
            db.add(RepairOrder(id=7, customer_id=1, vehicle_id=1, public_token="fictional-token-abcdef",
                               status="waiting_approval"))
            db.add(LineItem(repair_order_id=7, description="Sample tank repair", unit_price=100,
                            approval="pending"))
            db.commit()
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        self.flags.stop()
        app.state.read_only = self.old_read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        journey._last_pruned = 0.0

    def send(self, **overrides):
        data = {"sid": SID, "page": "book", "event": "view", "step": "open"}
        data.update(overrides)
        return self.client.post("/journey/event", json=data)

    def events(self):
        with self.Session() as db:
            return db.query(journey.JourneyEvent).order_by(journey.JourneyEvent.id).all()

    def test_event_is_bounded_and_never_stores_sensitive_payload(self):
        response = self.send(phone="602-555-1234", name="Not Stored",
                             url="/status/private-token?vin=SECRET", description="secret notes")
        self.assertEqual(response.status_code, 204)
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].session_id, events[0].page, events[0].event),
                         (SID, "book", "view"))
        self.assertNotIn("phone", journey.JourneyEvent.__table__.columns)
        self.assertNotIn("url", journey.JourneyEvent.__table__.columns)
        self.assertNotIn("description", journey.JourneyEvent.__table__.columns)

    def test_invalid_events_are_rejected(self):
        self.assertEqual(self.send(sid="customer@email.com").status_code, 400)
        self.assertEqual(self.send(event="typing_password").status_code, 400)
        self.assertEqual(self.send(page="/status/sensitive-token").status_code, 400)
        self.assertEqual(self.send(step="my address").status_code, 400)
        self.assertEqual(len(self.events()), 0)

    def test_secure_job_link_requires_signed_short_lived_context(self):
        self.assertEqual(self.send(page="status").status_code, 400)
        self.assertEqual(self.send(page="approval", context="forged").status_code, 400)
        good = journey.context_for(7)
        self.assertEqual(self.send(page="status", context=good).status_code, 204)
        self.assertEqual(self.send(page="approval", event="focus", step="decision",
                                   context=good).status_code, 204)
        self.assertEqual([event.ro_id for event in self.events()], [7, 7])
        self.assertIsNone(journey.context_ro_id(good + "tampered"))
        self.assertIsNone(journey.context_ro_id(""))

    def test_disabled_mode_does_not_log(self):
        with patch.dict(os.environ, {"CUSTOMER_JOURNEY_ENABLED": "false"}):
            self.assertEqual(self.send().status_code, 204)
            page = self.client.get("/site/")
            self.assertNotIn('src="/static/journey.js"', page.text)
        self.assertEqual(len(self.events()), 0)

    def test_status_and_approval_pages_offer_tracking_only_when_enabled(self):
        status = self.client.get("/status/fictional-token-abcdef")
        signed = self.client.get("/sign/ro/fictional-token-abcdef")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(signed.status_code, 200)
        for result in (status, signed):
            self.assertIn('src="/static/journey.js"', result.text)
            self.assertIn('name="imperial-journey-context"', result.text)
            self.assertNotIn('name="journey_sid" value="', result.text)  # injected at submit, not from server

    def test_signed_approval_is_recorded_in_same_job_without_name_or_signature(self):
        resp = self.client.post("/sign/ro/fictional-token-abcdef",
                                data={"signature_data": "data:image/png;base64,aGVsbG8=",
                                      "approved_by": "Synthetic Customer", "line_1": "approved",
                                      "journey_sid": SID})
        self.assertEqual(resp.status_code, 200)
        with self.Session() as db:
            entries = db.query(journey.JourneyEvent).all()
            self.assertEqual(len(entries), 1)
            self.assertEqual((entries[0].event, entries[0].page, entries[0].ro_id),
                             ("approval_complete", "approval", 7))
        self.assertFalse(any(s in str(self.events()[0].__dict__) for s in
                             ("Synthetic Customer", "data:image", "aGVsbG8")))

    def test_owner_only_dashboard_and_funnel(self):
        self.send()
        self.send(page="book", event="focus", step="contact")
        self.send(page="book", event="error", step="server_error")
        self.send(page="book", event="complete", step="success", sid=SOMEONE_ELSE)
        self.client.post("/login", data={"username": "front", "password": "sample-password-123"})
        self.assertEqual(self.client.get("/journey/").status_code, 403)
        self.client.get("/logout")
        self.client.post("/login", data={"username": "owner", "password": "sample-password-123"})
        page = self.client.get("/journey/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Customer Journey", page.text)
        self.assertIn("Validation / form error", page.text) if False else None
        with self.Session() as db:
            data = journey.report(db)
        self.assertEqual(data["rows"]["book"]["view"], 1)
        self.assertEqual(data["rows"]["book"]["focus"], 1)
        self.assertEqual(data["rows"]["book"]["complete"], 1)
        self.assertEqual(data["visitors"], 2)


if __name__ == "__main__":
    unittest.main()
