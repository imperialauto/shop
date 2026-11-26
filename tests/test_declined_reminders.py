"""Declined-work reminder 30 to 60 days later: a draft for the owner, never a send. Made-up data, private database."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app import declined_followups, text_approval
from app.database import Base
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound
from app.models import Customer, LineItem, RepairOrder, Vehicle
from app.routes import dispatch

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)  # a Tuesday, 11 AM in Phoenix


def thread(path, phone):
    return {"messages": [{"id": "AC1", "direction": "OUTBOUND"}, {"id": "AC3", "direction": "INBOUND"}]}, None


class DeclinedReminderTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.store_patch = patch.object(store, "SessionLocal", self.Session)
        self.store_patch.start()
        store.clear_cache()
        p = patch.object(declined_followups.customer_followups, "allowed_now", lambda *a: True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(store.clear_cache)
        self.addCleanup(self.store_patch.stop)

    def switch(self, on=True):
        with self.Session() as db:
            store.save(db, declined_followups.SETTING, "true" if on else "false", "owner")
            db.commit()
        store.clear_cache()

    def job(self, days_ago=35, cid=1, phone="(602) 555-0100", source="link", status="delivered",
            description="Front brake pads labor"):
        with self.Session() as db:
            if not db.get(Customer, cid):
                db.add(Customer(id=cid, first_name="Pat", last_name="Sample", phone=phone))
                db.add(Vehicle(id=cid, customer_id=cid, year=2016, make="Ford", model="Focus"))
            ro = RepairOrder(customer_id=cid, vehicle_id=cid, status=status)
            db.add(ro)
            db.flush()
            when = NOW - timedelta(days=days_ago)
            db.add_all([LineItem(repair_order_id=ro.id, description=description, item_type="labor",
                                 unit_price=150, approval="declined", approval_source=source, decided_at=when),
                        LineItem(repair_order_id=ro.id, description="Front brake pads", item_type="part",
                                 unit_price=60, approval="declined", approval_source=source, decided_at=when),
                        LineItem(repair_order_id=ro.id, description="Loyalty discount", item_type="discount",
                                 unit_price=-20, approval="declined", approval_source="shop", decided_at=when),
                        LineItem(repair_order_id=ro.id, description="Oil change labor", item_type="labor",
                                 unit_price=80, approval="approved", approval_source=source, decided_at=when)])
            db.commit()
            return ro.id

    def run_once(self, now=NOW):
        with self.Session() as db:
            item = declined_followups.queue_next(db, now, fetch=thread)
            return (item.quo_conversation_id, item.draft_reply, item.status, item.summary) if item else None

    def test_off_by_default(self):
        self.job()
        self.assertFalse(declined_followups.enabled())
        self.assertIsNone(self.run_once())
        self.switch(False)
        self.assertIsNone(self.run_once())
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).count(), 0)

    def test_drafts_once_for_the_owner_and_never_sends(self):
        self.switch()
        job = self.job()
        key, text, status, summary = self.run_once()
        self.assertEqual((key, status), ("LATER1", "needs_owner"))
        self.assertEqual(text, "Hi Pat, last month you passed on the Front brake pads for your 2016 Ford Focus. "
                               "Want us to take care of it? Reply here and we'll set up a time. - Imperial Auto Care")
        self.assertIn(f"#{job}", summary)
        self.assertIn("$210.00", summary)  # the discount line is left out
        self.assertIn("Draft only", summary)
        self.assertFalse(text_approval.is_quote(text))  # a "yes" back never approves work by itself
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
            self.assertEqual(dispatch.queued_replies(db), [])
            item = db.query(DispatchItem).one()
            self.assertEqual((item.category, item.last_message_id), ("declined_reminder", "AC3"))
            audit = db.query(DispatchAudit).filter_by(action=declined_followups.ACTION).one()
            self.assertNotIn("Pat", audit.detail)
            # The owner deals with it; the same declined work is never drafted again.
            item.status = "resolved"
            db.commit()
        self.assertIsNone(self.run_once(NOW + timedelta(days=1)))
        self.assertIsNone(self.run_once(NOW + timedelta(days=20)))
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).count(), 1)
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_skips_under_30_and_over_60_days(self):
        self.switch()
        self.job(days_ago=29)
        self.job(days_ago=61, cid=2, phone="(602) 555-0101")
        self.assertIsNone(self.run_once())
        self.job(days_ago=59, cid=3, phone="(602) 555-0102")
        self.assertEqual(self.run_once()[0], "LATER3")

    def test_says_a_couple_of_months_after_45_days(self):
        self.switch()
        self.job(days_ago=50)
        self.assertIn("a couple of months ago you passed on", self.run_once()[1])

    def test_work_done_here_since_is_left_out(self):
        self.switch()
        self.job()
        with self.Session() as db:
            later = RepairOrder(customer_id=1, vehicle_id=1, status="delivered")
            db.add(later)
            db.flush()
            db.add(LineItem(repair_order_id=later.id, description="Front brake pads and rotors labor", item_type="labor",
                            unit_price=225, approval="approved", decided_at=NOW - timedelta(days=10)))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_one_draft_per_customer_while_one_is_waiting(self):
        self.switch()
        self.job(days_ago=40)
        self.job(days_ago=35, description="Rear brake pads labor")
        first = self.run_once()
        self.assertIn("Front brake pads and Rear brake pads", first[1])  # both jobs, one text
        self.assertIsNone(self.run_once(NOW + timedelta(minutes=10)))

    def test_busy_customer_is_skipped(self):
        self.switch()
        self.job()
        with self.Session() as db:
            db.add(RepairOrder(customer_id=1, vehicle_id=1, status="in_progress"))
            db.commit()
        self.assertIsNone(self.run_once())


if __name__ == "__main__":
    unittest.main()
