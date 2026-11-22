"""Follow-up draft after a customer declines work on their link. Made-up data, private database, nothing is sent."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import quote_followups
from app.appointment_model import Appointment
from app.database import Base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Communication, Customer, LineItem, RepairOrder, Vehicle
from app.routes import dispatch

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)  # a Tuesday, 11 AM in Phoenix


def thread(path, phone):
    return {"messages": [{"id": "AC1", "direction": "OUTBOUND"}, {"id": "AC3", "direction": "INBOUND"}]}, None


class DeclinedFollowupTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        for target, value in ((quote_followups, "enabled"), (quote_followups.customer_followups, "allowed_now")):
            p = patch.object(target, value, lambda *a: True)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    def job(self, days_ago=4, status="approval_received", source="link", approved_too=False, cid=1,
            phone="(602) 555-0100"):
        with self.Session() as db:
            if not db.get(Customer, cid):
                db.add(Customer(id=cid, first_name="Pat", last_name="Sample", phone=phone))
                db.add(Vehicle(id=cid, customer_id=cid, year=2016, make="Ford", model="Focus"))
            ro = RepairOrder(customer_id=cid, vehicle_id=cid, status=status)
            db.add(ro)
            db.flush()
            when = NOW - timedelta(days=days_ago)
            db.add_all([LineItem(repair_order_id=ro.id, description="Front brake pads labor", item_type="labor",
                                 unit_price=150, approval="declined", approval_source=source, decided_at=when),
                        LineItem(repair_order_id=ro.id, description="Front brake pads", item_type="part",
                                 unit_price=60, approval="declined", approval_source=source, decided_at=when)])
            if approved_too:
                db.add(LineItem(repair_order_id=ro.id, description="Oil change", item_type="labor", unit_price=80,
                                approval="approved", approval_source=source, decided_at=when))
            db.commit()
            return ro.id

    def run_once(self, now=NOW, fetch=thread):
        with self.Session() as db:
            item = quote_followups.queue_next(db, now, fetch=fetch)
            return (item.quo_conversation_id, item.draft_reply, item.status, item.summary) if item else None

    def test_off_by_default(self):
        patch.stopall()
        self.assertFalse(quote_followups.enabled())

    def test_decline_makes_one_draft_for_the_owner_and_sends_nothing(self):
        job = self.job()
        key, text, status, summary = self.run_once()
        self.assertEqual(key, f"QUOTE{job}")
        self.assertEqual(status, "needs_owner")
        self.assertEqual(text, "Hi Pat, I wanted to check back on the Front brake pads we quoted for your 2016 Ford Focus. "
                               "If you'd like to take care of it, or want to talk through options, just reply here. Jae")
        self.assertIn("$210.00", summary)
        self.assertIn("Offer Snap Finance", summary)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
            self.assertEqual(dispatch.queued_replies(db), [])
            item = db.query(DispatchItem).one()
            self.assertEqual((item.last_message_id, item.thread_message_id), ("AC3", "AC3"))
        self.assertIsNone(self.run_once(NOW + timedelta(days=1)))  # never twice for the same job

    def test_too_soon_or_too_old(self):
        self.job(days_ago=1)
        self.job(days_ago=30, cid=2, phone="(602) 555-0101")
        self.assertIsNone(self.run_once())

    def test_declines_recorded_by_the_shop_are_left_alone(self):
        self.job(source="phone")
        self.assertIsNone(self.run_once())

    def test_part_approved_and_not_done_is_skipped(self):
        self.job(approved_too=True)
        self.assertIsNone(self.run_once())

    def test_part_approved_and_finished_still_gets_one(self):
        self.job(approved_too=True, status="delivered")
        self.assertIsNotNone(self.run_once())

    def test_customer_with_a_job_in_progress_is_skipped(self):
        self.job()
        with self.Session() as db:
            db.add(RepairOrder(customer_id=1, vehicle_id=1, status="in_progress"))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_booked_visit_recent_text_or_opt_out_skips(self):
        job = self.job()
        with self.Session() as db:
            db.add(Appointment(repair_order_id=job, resource_id="bay1", starts_at=NOW + timedelta(days=2),
                               ends_at=NOW + timedelta(days=2, hours=1), status="confirmed"))
            db.commit()
        self.assertIsNone(self.run_once())
        with self.Session() as db:
            db.query(Appointment).delete()
            db.add(Communication(customer_id=1, channel="sms", direction="inbound", body="ok thanks",
                                 created_at=(NOW - timedelta(days=1)).replace(tzinfo=None)))
            db.commit()
        self.assertIsNone(self.run_once())
        with self.Session() as db:
            db.query(Communication).delete()
            db.add(Communication(customer_id=1, channel="sms", direction="inbound", body="STOP",
                                 created_at=(NOW - timedelta(days=20)).replace(tzinfo=None)))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_open_conversation_with_the_shop_skips(self):
        self.job()
        with self.Session() as db:
            db.add(DispatchItem(quo_conversation_id="CN9", customer_phone="+16025550100", status="needs_owner",
                                summary="Question", last_message_id="AC2", created_at=NOW - timedelta(days=9),
                                updated_at=NOW - timedelta(days=9)))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_unreadable_thread_waits_for_a_later_tick(self):
        self.job()
        self.assertIsNone(self.run_once(fetch=lambda path, phone: (None, "unavailable")))
        self.assertIsNotNone(self.run_once())

    def test_many_lines_read_as_the_work(self):
        with self.Session() as db:
            lines = [LineItem(description=f"Job {n} labor", item_type="labor") for n in range(4)]
            self.assertEqual(quote_followups._items_text(lines), "work")
            self.assertEqual(quote_followups._items_text(lines[:2]), "Job 0 and Job 1")


if __name__ == "__main__":
    unittest.main()
