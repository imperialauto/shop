"""Day-after-pickup Google review text. Made-up data, private database, no live services."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import review_requests as reviews
from app.database import Base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Communication, Customer, Invoice, Payment, RepairOrder, Vehicle
from app.routes import business_settings_page, invoices
from app.routes.dispatch import queued_replies

LINK = "https://g.page/r/MadeUpLink123/review"
# Tuesday Nov 17 2026, 3 PM in Phoenix (UTC-7)
PICKUP = datetime(2026, 11, 17, 22, tzinfo=timezone.utc)
NEXT_DAY = PICKUP + timedelta(hours=20)  # Wednesday 11 AM in Phoenix


def thread(path, phone):
    assert (path, phone) == ("/conversation", "6025550100")
    return {"messages": [{"id": "AC1"}, {"id": "AC9"}]}, None


def unreadable(path, phone):
    return None, "unavailable"


class ReviewTextTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"))
        self.db.add(Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"))
        self.db.add(RepairOrder(id=5, customer_id=1, vehicle_id=1, status="delivered"))
        self.db.add(Invoice(id=9, repair_order_id=5, total=200, status="paid"))
        self.db.add(Payment(invoice_id=9, amount=200, method="zelle"))
        self.db.commit()
        self.on = True
        self.link = LINK
        for p in (patch.object(reviews, "enabled", lambda: self.on),
                  patch.object(reviews, "review_link", lambda: self.link if reviews.valid_link(self.link) else None)):
            p.start()
            self.addCleanup(p.stop)

    def ro(self, id=5):
        return self.db.get(RepairOrder, id)

    def due(self, now=NEXT_DAY, fetch=thread, allowed=True):
        return reviews.due(self.db, now, fetch=fetch, allowed=lambda _: allowed)

    def outbound(self):
        return [(o.kind, o.status) for o in self.db.query(DispatchOutbound).order_by(DispatchOutbound.id)]

    def test_off_by_default_schedules_and_sends_nothing(self):
        self.on = False
        self.assertIsNone(reviews.schedule(self.db, self.ro(), PICKUP))
        self.assertIsNone(self.due())
        self.assertEqual(self.outbound(), [])

    def test_next_shop_day_text_joins_the_reply_queue(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.assertIsNone(self.due(PICKUP + timedelta(hours=2)))  # same day: waits
        item = self.due()
        self.assertEqual(item.status, "approved")
        self.assertEqual(item.draft_reply,
                         "Hey Pat, thanks again for trusting us with your 2016 Ford Focus. If we took good care of you, "
                         f"would you leave us a quick Google review? It really helps a small shop. {LINK} Jae")
        self.assertEqual(item.thread_message_id, "AC9")
        self.assertEqual(self.outbound(), [("review_request", "sent"), ("dashboard_reply", "queued")])
        [reply] = queued_replies(self.db)
        self.assertEqual((reply["phone"], reply["messageId"]), ("+16025550100", "AC9"))

    def test_outside_shop_hours_waits(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.assertIsNone(self.due(allowed=False))
        self.assertEqual(self.outbound(), [("review_request", "queued")])

    def test_once_per_customer_ever(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.due()
        self.db.add(RepairOrder(id=6, customer_id=1, vehicle_id=1, status="delivered"))
        self.db.commit()
        self.assertIsNone(reviews.schedule(self.db, self.ro(6), NEXT_DAY))
        self.assertEqual(len(self.outbound()), 2)

    def test_marked_reviewed_is_never_asked(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        reviews.mark_reviewed(self.db, 1, "owner")
        self.db.commit()
        self.assertIsNone(self.due())
        self.assertNotIn(("dashboard_reply", "queued"), self.outbound())
        self.assertIsNone(reviews.schedule(self.db, self.ro(), PICKUP))

    def test_unpaid_balance_waits_and_old_pickups_expire(self):
        self.db.query(Payment).delete()
        self.db.commit()
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.assertIsNone(self.due())
        self.assertEqual(self.outbound(), [("review_request", "queued")])
        self.assertIsNone(self.due(PICKUP + timedelta(days=8)))
        self.assertEqual(self.outbound(), [("review_request", "skipped")])
        self.assertFalse(reviews.asked_before(self.db, 1))  # too old to ask is not an ask

    def test_customer_texted_since_pickup_waits_for_owner(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.db.add(Communication(customer_id=1, channel="sms", direction="inbound", body="the noise is back",
                                  created_at=(PICKUP + timedelta(hours=3)).replace(tzinfo=None)))
        self.db.commit()
        item = self.due()
        self.assertEqual(item.status, "needs_owner")
        self.assertEqual(self.outbound(), [("review_request", "skipped")])
        self.assertTrue(reviews.asked_before(self.db, 1))  # a draft held for the owner counts as asked

    def test_unreadable_thread_or_waived_balance_waits_for_owner(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.assertEqual(self.due(fetch=unreadable).status, "needs_owner")
        self.db.add(Customer(id=2, first_name="Lee", last_name="Sample", phone="(602) 555-0100"))
        self.db.add(RepairOrder(id=7, customer_id=2, vehicle_id=1, status="delivered"))
        self.db.add(Invoice(id=10, repair_order_id=7, total=100, waived_amount=100, status="paid"))
        self.db.commit()
        reviews.schedule(self.db, self.ro(7), PICKUP)
        self.assertEqual(self.due().status, "needs_owner")

    def test_no_real_link_sends_nothing(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.link = reviews.PLACEHOLDER
        self.assertIsNone(self.due())
        self.assertFalse(reviews.valid_link("http://g.page/r/x/review"))
        self.assertFalse(reviews.valid_link("https://evil.example/r/x"))
        self.assertTrue(reviews.valid_link(LINK))

    def test_owner_number_and_opt_out_are_skipped(self):
        reviews.schedule(self.db, self.ro(), PICKUP)
        self.db.add(Communication(customer_id=1, channel="sms", direction="inbound", body="STOP",
                                  created_at=(PICKUP - timedelta(days=30)).replace(tzinfo=None)))
        self.db.commit()
        self.assertIsNone(self.due())
        self.assertEqual(self.outbound(), [("review_request", "skipped")])

    def test_paid_at_counter_pickup_schedules(self):
        self.db.add(RepairOrder(id=8, customer_id=1, vehicle_id=1, status="complete"))
        invoice = Invoice(id=11, repair_order_id=8, total=50, status="sent")
        self.db.add(invoice)
        self.db.commit()
        invoice.payments.append(Payment(amount=50, method="cash"))
        ro = invoices._settle(invoice, picked_up=True)
        self.assertEqual(ro.status, "delivered")
        invoices._schedule_review(self.db, ro)
        self.assertEqual(self.outbound(), [("review_request", "queued")])


class ReviewSettingsTests(unittest.TestCase):
    def parse(self, **form):
        base = {"automatic_review_text_shown": "1"}
        with patch.object(business_settings_page, "pricing") as pricing:
            pricing.RATE_LABELS = {}
            try:
                return business_settings_page.parse({**base, **form})
            except business_settings_page.Invalid as exc:
                return str(exc)

    def test_link_must_be_a_google_review_link(self):
        self.assertTrue(reviews.valid_link(reviews.DEFAULT_LINK))
        self.assertIn("starts with https://g.page/r/", self.parse(google_review_link="https://example.com/x"))
