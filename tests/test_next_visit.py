"""10% off labor on the next visit. Made-up data, private database, no live services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import next_visit, referrals
from app.database import Base
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, Vehicle

NOW = datetime(2026, 11, 22, 18, tzinfo=timezone.utc)


class NextVisitTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.on = True
        p = patch.object(next_visit, "enabled", lambda: self.on)
        p.start()
        self.addCleanup(p.stop)
        self.db.add_all([Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                         Vehicle(id=1, customer_id=1, make="Ford")])
        self.db.commit()

    def job(self, *lines):
        ro = RepairOrder(customer_id=1, vehicle_id=1, status="complete")
        self.db.add(ro)
        self.db.flush()
        for kind, price in lines:
            self.db.add(LineItem(repair_order_id=ro.id, description=kind, item_type=kind, unit_price=price, approval="approved"))
        self.db.commit()
        self.db.refresh(ro)
        return ro

    def paid(self, ro, days_ago):
        total = sum(li.total for li in ro.line_items)
        inv = Invoice(repair_order_id=ro.id, total=total, subtotal=total, status="paid")
        self.db.add(inv)
        self.db.flush()
        inv.payments.append(Payment(amount=total, method="cash", paid_at=(NOW - timedelta(days=days_ago)).replace(tzinfo=None)))
        self.db.commit()
        return inv

    def apply(self, ro):
        return next_visit.apply(self.db, ro, "office", now=NOW)

    def test_ten_percent_of_labor_capped_at_50_once(self):
        first = self.job(("labor", 150))
        inv = self.paid(first, 10)
        self.assertIn("Good through February 10, 2027", next_visit.receipt_line(inv))
        second = self.job(("labor", 300), ("part", 400))
        self.assertEqual(self.apply(second), "Next-visit offer: 10% off labor $30.00")
        self.assertEqual(sum(li.total for li in second.line_items), 670)
        self.paid(second, 1)
        third = self.job(("labor", 900))
        self.assertIsNone(self.apply(third))  # the job that used it earned no new one
        self.assertIsNone(next_visit.receipt_line(self.db.query(Invoice).filter_by(repair_order_id=second.id).one()))

    def test_cap_and_minimum(self):
        self.paid(self.job(("labor", 80)), 5)
        self.assertIsNone(self.apply(self.job(("labor", 150), ("part", 50))))  # $200 is not over $200
        big = self.job(("labor", 800))
        self.apply(big)
        self.assertEqual(min(li.total for li in big.line_items), -50)

    def test_expires_after_90_days(self):
        self.paid(self.job(("labor", 80)), 91)
        self.assertIsNone(self.apply(self.job(("labor", 400))))

    def test_one_offer_per_job_with_referral(self):
        self.paid(self.job(("labor", 80)), 5)
        ro = self.job(("labor", 400), (referrals.DISCOUNT, -25))
        self.assertIsNone(self.apply(ro))

    def test_off_does_nothing(self):
        self.on = False
        inv = self.paid(self.job(("labor", 80)), 5)
        self.assertIsNone(self.apply(self.job(("labor", 400))))
        self.assertIsNone(next_visit.receipt_line(inv))
