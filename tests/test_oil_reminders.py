"""Oil change due reminder. Made-up data, private database, nothing is sent."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import oil_reminders
from app.appointment_model import Appointment
from app.database import Base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Communication, Customer, Invoice, LineItem, Payment, RepairOrder, Vehicle
from app.routes import dispatch

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)  # a Tuesday, 11 AM in Phoenix


def thread(path, phone):
    return {"messages": [{"id": "AC3", "direction": "OUTBOUND"}]}, None


class OilReminderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        for target, value in ((oil_reminders, "enabled"), (oil_reminders.customer_followups, "allowed_now")):
            p = patch.object(target, value, lambda *a: True)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    def job(self, days_ago=160, description="Full synthetic oil change", status="delivered", phone="(602) 555-0100", cid=1):
        with self.Session() as db:
            if not db.get(Customer, cid):
                db.add(Customer(id=cid, first_name="Pat", last_name="Sample", phone=phone))
                db.add(Vehicle(id=cid, customer_id=cid, year=2016, make="Ford", model="Focus"))
            ro = RepairOrder(customer_id=cid, vehicle_id=cid, status=status)
            db.add(ro)
            db.flush()
            db.add(LineItem(repair_order_id=ro.id, description=description, item_type="labor", unit_price=80))
            inv = Invoice(repair_order_id=ro.id, total=80, status="paid")
            db.add(inv)
            db.flush()
            db.add(Payment(invoice_id=inv.id, amount=80, paid_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None)))
            db.commit()
            return ro.id

    def run_once(self, now=NOW):
        with self.Session() as db:
            item = oil_reminders.queue_next(db, now, fetch=thread)
            return (item.quo_conversation_id, item.draft_reply) if item else None

    def test_off_by_default(self):
        patch.stopall()
        self.assertFalse(oil_reminders.enabled())
        self.assertEqual(oil_reminders.months(), 5)

    def test_due_oil_change_queues_one_reminder(self):
        job = self.job()
        key, text = self.run_once()
        self.assertEqual(key, f"OIL{job}")
        self.assertEqual(text, "Hi Pat, it's been about 5 months since the last oil change on your 2016 Ford Focus, "
                               "so it's probably coming due. Want me to get you on the schedule? Jae")
        with self.Session() as db:
            replies = dispatch.queued_replies(db)
            self.assertEqual([r["messageId"] for r in replies], ["AC3"])
        self.assertIsNone(self.run_once(NOW + timedelta(days=1)))  # never twice for the same oil change

    def test_not_yet_due_or_too_old(self):
        self.job(days_ago=100)
        self.job(days_ago=400, cid=2, phone="(602) 555-0101")
        self.assertIsNone(self.run_once())

    def test_only_oil_changes_count(self):
        self.job(description="Front brake pads")
        self.assertIsNone(self.run_once())

    def test_newer_oil_change_resets_the_clock(self):
        self.job(days_ago=160)
        self.job(days_ago=20)
        self.assertIsNone(self.run_once())

    def test_open_job_skips(self):
        self.job()
        self.job(days_ago=1, description="Check engine light", status="diagnosing")
        self.assertIsNone(self.run_once())

    def test_booked_visit_skips(self):
        job = self.job()
        with self.Session() as db:
            db.add(Appointment(repair_order_id=job, resource_id="bay1", starts_at=NOW + timedelta(days=2),
                               ends_at=NOW + timedelta(days=2, hours=1), status="confirmed"))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_recent_text_or_opt_out_skips(self):
        self.job()
        with self.Session() as db:
            db.add(Communication(customer_id=1, channel="sms", direction="outbound", body="Your car is ready.",
                                 created_at=(NOW - timedelta(days=2)).replace(tzinfo=None)))
            db.commit()
        self.assertIsNone(self.run_once())
        self.job(cid=2, phone="(602) 555-0101")
        with self.Session() as db:
            db.add(Communication(customer_id=2, channel="sms", direction="inbound", body="STOP",
                                 created_at=(NOW - timedelta(days=60)).replace(tzinfo=None)))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_waiting_conversation_skips(self):
        self.job()
        with self.Session() as db:
            db.add(DispatchItem(quo_conversation_id="CN9", customer_phone="+16025550100", status="needs_owner",
                                summary="Question about tires", last_message_id="AC2",
                                created_at=NOW - timedelta(days=30), updated_at=NOW - timedelta(days=30)))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_one_per_five_minutes(self):
        self.job()
        self.job(cid=2, phone="(602) 555-0101")
        self.assertIsNotNone(self.run_once())
        self.assertIsNone(self.run_once(NOW + timedelta(minutes=2)))
        self.assertIsNotNone(self.run_once(NOW + timedelta(minutes=6)))

    def test_unreadable_thread_sends_nothing(self):
        self.job()
        with self.Session() as db:
            self.assertIsNone(oil_reminders.queue_next(db, NOW, fetch=lambda p, n: (None, "unavailable")))
            self.assertEqual(db.query(DispatchOutbound).count(), 0)


if __name__ == "__main__":
    unittest.main()
