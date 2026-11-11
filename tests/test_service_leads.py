"""Synthetic, isolated tests for service leads that survive communication closeout."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.appointment_model import Appointment
from app.dispatch_model import DispatchItem
from app.lead_model import ServiceLead, ServiceLeadAudit
from app.lead_lifecycle import (
    confirm_booking, due_lead_digest, record_customer_decline,
    reopen_cancelled_booking,
)
from app.main import app
from app.models import Customer, RepairOrder, User, Vehicle
from app.routes import dispatch, internal_messaging

KEY = "fixture-internal-key"
HEADERS = {"X-Internal-Key": KEY}
NOW = datetime(2026, 11, 6, 18, 30, tzinfo=timezone.utc)  # Fri 11:30 Phoenix


class ServiceLeadTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.flag = patch.dict(os.environ, {"SERVICE_LEADS_ENABLED": "true"})
        self.flag.start()
        self.key = patch.object(internal_messaging, "INTERNAL_API_KEY", KEY)
        self.key.start()
        self.client = TestClient(app)
        with self.Session() as db:
            db.add(User(id=1, username="test", password_hash="x", role="owner"))
            db.add(Customer(id=1, first_name="Synthetic", last_name="Customer", phone="6025550100"))
            db.commit()

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        self.key.stop()
        self.flag.stop()
        self.engine.dispose()

    def post(self, message_id="m1", body="2015 Nissan Sentra needs starter repair", **extra):
        return self.client.post("/internal/dispatch/intake", headers=HEADERS, json={
            "conversationId": "CNsynthetic", "messageId": message_id,
            "phone": "+16025550100", "body": body, **extra,
        })

    def leads(self):
        with self.Session() as db:
            return db.query(ServiceLead).order_by(ServiceLead.id).all()

    def test_answered_sona_call_opens_one_lead_and_dedupes_replay(self):
        first = self.post(kind="call", callOutcome="answered")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["recorded"])
        self.assertEqual(len(self.leads()), 1)
        lead = self.leads()[0]
        self.assertEqual((lead.source, lead.customer_id, lead.status),
                         ("quo", 1, "new"))
        self.assertEqual(lead.last_event_id, "m1")
        duplicate = self.post(kind="call", callOutcome="answered")
        self.assertTrue(duplicate.json()["duplicate"])
        with self.Session() as db:
            self.assertEqual(db.query(ServiceLeadAudit).count(), 1)

    def test_comm_reply_and_done_close_dispatch_but_not_service_opportunity(self):
        self.post()
        with self.Session() as db:
            item = db.query(DispatchItem).one()
            closed = dispatch._mark_done(db, [item.id])
            self.assertIn("Closed", closed)
        self.assertEqual(self.leads()[0].status, "new")
        self.post(message_id="m2", body="Can you give me a price?")
        with self.Session() as db:
            item = db.query(DispatchItem).filter(DispatchItem.status == "needs_owner").first()
            self.assertIsNotNone(item)
            response = self.client.post("/internal/dispatch/handled", headers=HEADERS, json={
                "conversationId": "CNsynthetic", "at": "2099-01-01T00:00:00Z",
            })
            self.assertTrue(response.json()["closed"])
        self.assertEqual(len(self.leads()), 1)
        self.assertNotIn(self.leads()[0].status, ("booked", "declined"))

    def test_old_lead_remains_open_but_unchanged_digest_is_not_spammed(self):
        self.post()
        with self.Session() as db:
            lead = db.query(ServiceLead).one()
            lead.next_action_due_at = NOW - timedelta(days=4)
            db.commit()
            first = due_lead_digest(db, NOW)
            later = due_lead_digest(db, NOW + timedelta(minutes=30))
            self.assertIn("1 service leads", first[1])
            self.assertEqual(first[0], later[0])
            self.assertEqual(lead.status, "new")
            self.assertTrue(dispatch.reserve(db, first[0], "owner_leads"))
            self.assertFalse(dispatch.reserve(db, later[0], "owner_leads"),
                             "unchanged half-hour digest should not be reserved twice")
            lead.next_action_due_at = NOW - timedelta(days=3)
            db.commit()
            self.assertEqual(due_lead_digest(db, NOW + timedelta(hours=1))[0], first[0],
                             "timer movement alone is not a new customer interaction")
            lead.last_event_id = "genuine-new-customer-message"
            db.commit()
            changed = due_lead_digest(db, NOW + timedelta(hours=1))
            self.assertNotEqual(first[0], changed[0],
                                "new customer activity must alert promptly")
            self.assertTrue(dispatch.reserve(db, changed[0], "owner_leads"))
            following_day = due_lead_digest(db, NOW + timedelta(days=1, hours=1))  # Saturday 12:30 Phoenix
            self.assertNotEqual(changed[0], following_day[0],
                                "daily follow-up remains available for unresolved work")

    def test_unmodified_multi_lead_digest_key_ignores_due_order(self):
        first = self.post(message_id="lead-a", body="Brake repair quote needed")
        second = self.post(message_id="lead-b", body="Starter replacement estimate needed",
                           conversationId="CNsynthetic2", phone="+16025550101")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        with self.Session() as db:
            leads = db.query(ServiceLead).order_by(ServiceLead.id).all()
            self.assertEqual(len(leads), 2)
            leads[0].next_action_due_at = NOW - timedelta(days=3)
            leads[1].next_action_due_at = NOW - timedelta(days=2)
            db.commit()
            before = due_lead_digest(db, NOW)
            self.assertIsNotNone(before)
            # The same two unresolved conversations are still outstanding,
            # but their due timestamps were shuffled by a routine scheduler.
            leads[0].next_action_due_at = NOW - timedelta(days=1)
            leads[1].next_action_due_at = NOW - timedelta(days=4)
            db.commit()
            after = due_lead_digest(db, NOW + timedelta(minutes=30))
            self.assertEqual(before[0], after[0],
                             "due-only reordering cannot trigger a duplicate owner SMS")

    def test_tentative_booking_cannot_close_lead_but_confirmed_can(self):
        self.post()
        with self.Session() as db:
            db.add(Vehicle(id=1, customer_id=1, year=2015, make="Nissan", model="Sentra"))
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, concern="Synthetic"))
            db.add(Appointment(id=1, repair_order_id=1, resource_id="bay1",
                               starts_at=NOW + timedelta(days=1),
                               ends_at=NOW + timedelta(days=1, hours=1), status="tentative"))
            db.commit()
            lead = db.query(ServiceLead).one()
            with self.assertRaises(ValueError):
                confirm_booking(db, lead.id, 1, "test-advisor", NOW)
            db.get(Appointment, 1).status = "confirmed"
            confirmed = confirm_booking(db, lead.id, 1, "test-advisor", NOW)
            self.assertEqual(confirmed.status, "booked")
            db.commit()
            self.assertIsNone(due_lead_digest(db, NOW))
            db.get(Appointment, 1).status = "cancelled"
            count = reopen_cancelled_booking(db, 1, "test-advisor", NOW)
            db.commit()
            self.assertEqual(count, 1)
            self.assertEqual(db.get(ServiceLead, lead.id).status, "scheduling")
            self.assertIsNotNone(due_lead_digest(db, NOW))

    def test_decline_must_be_explicit_and_source_linked(self):
        self.post()
        with self.Session() as db:
            lead = db.query(ServiceLead).one()
            with self.assertRaises(ValueError):
                record_customer_decline(db, lead.id, "", "advisor", True, NOW)
            with self.assertRaises(ValueError):
                record_customer_decline(db, lead.id, "quo:synthetic-ref", "advisor", False, NOW)
            record_customer_decline(db, lead.id, "quo:synthetic-ref", "advisor", True, NOW)
            db.commit()
            self.assertEqual(db.get(ServiceLead, lead.id).status, "declined")
            self.assertIsNone(due_lead_digest(db, NOW))

    def test_missed_call_without_transcript_still_opens_a_lead(self):
        result = self.post(message_id="missing-transcript", body="Missed call, no voicemail.",
                           kind="call", callOutcome="missed")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.leads()), 1)
        self.assertEqual(self.leads()[0].status, "new")
        duplicate = self.post(message_id="missing-transcript", body="Missed call, no voicemail.",
                              kind="call", callOutcome="missed")
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(len(self.leads()), 1)

    def test_information_only_call_does_not_create_service_lead(self):
        self.post(body="What are your business hours?", kind="call", callOutcome="answered")
        self.assertEqual(self.leads(), [])

    def test_feature_remains_off_without_explicit_enablement(self):
        with patch.dict(os.environ, {"SERVICE_LEADS_ENABLED": "false"}):
            self.post()
        self.assertEqual(self.leads(), [])


if __name__ == "__main__":
    unittest.main()


class LeadAlertSwitchTests(unittest.TestCase):
    def test_owner_can_turn_off_lead_reminders(self):
        from datetime import datetime
        from app import alert_rules
        self.assertIn("owner_leads", alert_rules.OPTIONAL)
        rules = {**alert_rules.DEFAULTS, "off": ["owner_leads"]}
        self.assertFalse(alert_rules.allowed("owner_leads", False, False, datetime(2026, 11, 9, 12, 0), rules))
        self.assertTrue(alert_rules.allowed("owner_leads", True, False, datetime(2026, 11, 9, 12, 0), rules))  # urgent still goes
