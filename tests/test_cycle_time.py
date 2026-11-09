"""First contact to paid, per job and typical. Made-up data."""
import unittest
from datetime import datetime, timedelta, timezone

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import cycle_time
from app.database import Base
from app.dispatch_model import DispatchItem
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, Vehicle

T0 = datetime(2026, 11, 2, 16, 0)


class CycleTimeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                        Vehicle(id=1, customer_id=1, make="Ford", model="Focus"),
                        DispatchItem(quo_conversation_id="CN1", customer_phone="+16025550100", customer_id=1, category="estimate",
                                     summary="Brakes?", last_message_id="m1", created_at=T0.replace(tzinfo=timezone.utc)),
                        RepairOrder(id=5, customer_id=1, vehicle_id=1, created_at=T0 + timedelta(hours=2)),
                        LineItem(repair_order_id=5, description="Pads", unit_price=349, approval="approved", decided_at=T0 + timedelta(hours=5)),
                        Invoice(id=9, repair_order_id=5, total=349, status="paid", created_at=T0 + timedelta(days=2)),
                        Payment(invoice_id=9, amount=349, method="card", paid_at=T0 + timedelta(days=2, hours=1))])
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_job_milestones_from_first_text(self):
        with self.Session() as db:
            t = cycle_time.for_job(db, db.get(RepairOrder, 5))
        self.assertEqual([s["label"] for s in t["steps"]], ["First contact", "Job opened", "Customer approved", "Invoiced", "Paid"])
        self.assertEqual([s["hours"] for s in t["steps"]], [0.0, 2.0, 5.0, 48.0, 49.0])
        self.assertEqual(t["hours_to_paid"], 49.0)

    def test_typical_time_over_last_30_days(self):
        with self.Session() as db:
            r = cycle_time.recent(db, now=datetime(2026, 11, 9, tzinfo=timezone.utc))
            self.assertEqual((r["jobs"], r["median_hours"]), (1, 49.0))
            self.assertEqual(cycle_time.recent(db, now=datetime(2026, 12, 30, tzinfo=timezone.utc))["jobs"], 0)

    def test_unpaid_job_has_no_total(self):
        with self.Session() as db:
            db.query(Payment).delete()
            db.commit()
            self.assertIsNone(cycle_time.for_job(db, db.get(RepairOrder, 5))["hours_to_paid"])


if __name__ == "__main__":
    unittest.main()
