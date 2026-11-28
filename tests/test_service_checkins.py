"""After-service check-in a week after pickup. Made-up data, private database, no live services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import job_events, service_checkins as checkins
from app.database import Base
from app.dispatch_model import DispatchOutbound
from app.models import Communication, Customer, Invoice, Payment, RepairOrder, Vehicle

PICKUP = datetime(2026, 11, 17, 22, tzinfo=timezone.utc)  # Tuesday 3 PM in Phoenix
WEEK_LATER = PICKUP + timedelta(days=6, hours=20)  # next Tuesday 11 AM


def thread(path, phone):
    assert (path, phone) == ("/conversation", "6025550100")
    return {"messages": [{"id": "AC1"}, {"id": "AC9"}]}, None


def unreadable(path, phone):
    return None, "unavailable"


class ServiceCheckinTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.db.add_all([Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"),
                         Vehicle(id=1, customer_id=1, year=2013, make="Chevrolet", model="Impala"),
                         RepairOrder(id=13, customer_id=1, vehicle_id=1, status="delivered"),
                         Invoice(id=9, repair_order_id=13, total=200, status="paid")])
        self.db.flush()
        self.db.add(Payment(invoice_id=9, amount=200, method="zelle"))
        self.db.commit()
        self.on = True
        p = patch.object(checkins, "enabled", lambda: self.on)
        p.start()
        self.addCleanup(p.stop)

    def ro(self):
        return self.db.get(RepairOrder, 13)

    def due(self, now=WEEK_LATER, fetch=thread):
        return checkins.due(self.db, now, fetch=fetch, allowed=lambda _: True)

    def outbound(self):
        return [(o.kind, o.status) for o in self.db.query(DispatchOutbound).order_by(DispatchOutbound.id)]

    def test_off_by_default(self):
        self.on = False
        self.assertIsNone(checkins.schedule(self.db, self.ro(), PICKUP))
        self.assertIsNone(self.due())
        self.assertEqual(self.outbound(), [])

    def test_pickup_schedules_it_through_job_events(self):
        self.assertIn(checkins.schedule, job_events.HANDLERS["delivered"])

    def test_week_after_pickup_joins_reply_queue(self):
        checkins.schedule(self.db, self.ro(), PICKUP)
        self.assertIsNone(self.due(PICKUP + timedelta(days=5)))
        item = self.due()
        self.assertEqual(item.status, "approved")
        self.assertEqual(item.draft_reply, "Hi Pat, it's Jae at Imperial Auto Care. Checking in on your 2013 Chevrolet "
                                           "Impala since your visit last week. Is everything running right? "
                                           "If anything seems off, just reply here.")
        self.assertEqual(self.outbound(), [("service_checkin", "sent"), ("dashboard_reply", "queued")])
        self.assertIsNone(self.due())  # once per pickup

    def test_held_for_owner_when_they_texted_or_thread_unreadable(self):
        checkins.schedule(self.db, self.ro(), PICKUP)
        self.db.add(Communication(customer_id=1, channel="sms", direction="inbound", body="Thanks!",
                                  created_at=(PICKUP + timedelta(days=1)).replace(tzinfo=None)))
        self.db.commit()
        self.assertEqual(self.due().status, "needs_owner")
        self.assertEqual(self.outbound(), [("service_checkin", "skipped")])

    def test_unreadable_thread_waits_for_owner(self):
        checkins.schedule(self.db, self.ro(), PICKUP)
        self.assertEqual(self.due(fetch=unreadable).status, "needs_owner")

    def test_old_pickups_and_customers_back_in_are_skipped(self):
        checkins.schedule(self.db, self.ro(), PICKUP)
        self.assertIsNone(self.due(PICKUP + timedelta(days=15)))
        self.assertEqual(self.outbound(), [("service_checkin", "skipped")])
        self.db.query(DispatchOutbound).delete()
        self.db.add(RepairOrder(id=14, customer_id=1, vehicle_id=1, status="in_progress"))
        self.db.commit()
        checkins.schedule(self.db, self.ro(), PICKUP)
        self.assertIsNone(self.due())
        self.assertEqual(self.outbound(), [("service_checkin", "skipped")])


if __name__ == "__main__":
    unittest.main()
