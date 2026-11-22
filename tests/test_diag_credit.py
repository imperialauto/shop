"""Diag credit reminder draft. Made-up data, private database, nothing is sent."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import diag_credit, quote_followups
from app.database import Base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, Vehicle

NOW = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)  # a Tuesday, 11 AM in Phoenix


def thread(path, phone):
    return {"messages": [{"id": "AC4", "direction": "OUTBOUND"}]}, None


class DiagCreditTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        for target, value in ((diag_credit, "enabled"), (quote_followups, "enabled"),
                              (diag_credit.customer_followups, "allowed_now")):
            p = patch.object(target, value, lambda *a: True)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)
        with self.Session() as db:
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"))
            db.add(Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"))
            db.commit()

    def job(self, days_ago=3, status="delivered", extra=None):
        with self.Session() as db:
            ro = RepairOrder(customer_id=1, vehicle_id=1, status=status)
            db.add(ro)
            db.flush()
            db.add(LineItem(repair_order_id=ro.id, description="Diagnostic", item_type="labor", unit_price=150))
            if extra:
                db.add(LineItem(repair_order_id=ro.id, **extra))
            inv = Invoice(repair_order_id=ro.id, total=150, status="paid")
            db.add(inv)
            db.flush()
            db.add(Payment(invoice_id=inv.id, amount=150, paid_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None)))
            db.commit()
            return ro.id

    def run_once(self, now=NOW):
        with self.Session() as db:
            item = diag_credit.queue_next(db, now, fetch=thread)
            return (item.quo_conversation_id, item.draft_reply, item.status) if item else None

    def test_off_by_default(self):
        patch.stopall()
        self.assertFalse(diag_credit.enabled())

    def test_diag_only_visit_gets_one_draft_and_nothing_is_sent(self):
        job = self.job()
        key, text, status = self.run_once()
        self.assertEqual((key, status), (f"DIAG{job}", "needs_owner"))
        self.assertEqual(text, "Hi Pat, just a reminder that the $75 from the diagnostic on your 2016 Ford Focus still goes "
                               "toward the repair if you'd like us to take care of it. Just reply here and I'll get you "
                               "on the schedule. Jae")
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
        self.assertIsNone(self.run_once(NOW + timedelta(days=1)))

    def test_repair_done_on_the_same_job_is_skipped(self):
        self.job(extra=dict(description="Front brake pads", item_type="labor", unit_price=150))
        self.assertIsNone(self.run_once())

    def test_too_soon_too_old_or_not_finished(self):
        self.job(days_ago=1)
        self.assertIsNone(self.run_once())

    def test_customer_who_came_back_is_skipped(self):
        self.job()
        with self.Session() as db:
            db.add(RepairOrder(customer_id=1, vehicle_id=1, status="delivered"))
            db.commit()
        self.assertIsNone(self.run_once())

    def test_link_decline_gets_one_text_that_carries_the_credit(self):
        self.job(extra=dict(description="Water pump labor", item_type="labor", unit_price=300, approval="declined",
                            approval_source="link", decided_at=NOW - timedelta(days=4)))
        self.assertIsNone(self.run_once())
        with self.Session() as db:
            item = quote_followups.queue_next(db, NOW, fetch=thread)
            self.assertIn("The $75 from your diagnostic still goes toward it.", item.draft_reply)
            self.assertIn("Water pump", item.draft_reply)


if __name__ == "__main__":
    unittest.main()
