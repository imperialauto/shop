"""Synthetic coverage for the follow-up scheduler. No external requests."""
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import approval_links, customer_followups as followups
from app.appointment_model import Appointment
from app.database import Base
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound
from app.models import Communication, Customer, LineItem, RepairOrder, Vehicle


class CustomerFollowupTests(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.now = datetime(2026, 11, 16, 20, tzinfo=timezone.utc)
        self.customer = Customer(first_name="Synthetic", last_name="Customer", phone="+16025550172")
        self.db.add(self.customer)
        self.db.flush()
        self.item = DispatchItem(customer_id=self.customer.id, customer_phone=self.customer.phone,
                                 quo_conversation_id="thread-test", category="pricing", last_message_id="request",
                                 updated_at=self.now - timedelta(days=4))
        self.db.add(self.item)

    def sms(self, event, hours, direction, body):
        self.db.add(Communication(customer_id=self.customer.id, channel="sms", external_id=event,
                                  direction=direction, body=body, created_at=self.now - timedelta(hours=hours)))
        self.db.flush()

    def order(self, status):
        vehicle = Vehicle(customer_id=self.customer.id, make="Ford")
        self.db.add(vehicle)
        self.db.flush()
        order = RepairOrder(customer_id=self.customer.id, vehicle_id=vehicle.id, status=status, public_token="synthetic-token")
        self.db.add(order)
        self.db.flush()
        return order

    def candidate(self):
        return followups.candidate(self.db, self.customer, self.now)

    def test_24_hour_reply_and_72_hour_reactivation_use_distinct_keys(self):
        self.sms("customer-question", 23, "inbound", "What is the price for brakes?")
        self.assertIsNone(self.candidate())
        self.now += timedelta(hours=1)
        first = self.candidate()
        self.assertEqual(first["reason"], "unanswered")
        with patch.object(followups, "allowed_now", return_value=True):
            self.assertEqual(followups.due(self.db, self.now)[0]["key"], first["key"])
            self.assertEqual(followups.due(self.db, self.now), [])
        self.assertEqual(self.db.query(DispatchOutbound).count(), 1)
        self.sms("automatic-reply", 0, "outbound", first["text"])
        self.assertIsNone(self.candidate())
        self.now += timedelta(days=2)
        second = self.candidate()
        self.assertEqual(second["reason"], "reactivate")
        self.assertNotEqual(first["key"], second["key"])
        self.sms("automatic-reactivation", 0, "outbound", second["text"])
        self.assertIsNone(self.candidate())

    def test_waiting_on_customer_and_opt_out(self):
        self.sms("shop-reply", 26, "outbound", "What year is your vehicle?")
        self.assertEqual(self.candidate()["reason"], "waiting")
        self.sms("opt-out", 25, "inbound", "STOP")
        self.assertIsNone(self.candidate())

    def test_estimate_requires_outbound_evidence_offsite_and_48_hours(self):
        order = self.order("waiting_approval")
        self.db.add(LineItem(repair_order_id=order.id, item_type="labor", description="Fixture",
                             quantity=1, unit_price=100, approval="pending"))
        self.db.add(DispatchItem(quo_conversation_id=approval_links.conversation_key(order.id),
                                 customer_id=self.customer.id, customer_phone=self.customer.phone,
                                 category="approval_link", last_message_id="question", status="approved"))
        self.sms("sent-estimate", 47, "outbound", "Your estimate is ready: https://example.test/status/synthetic-token")
        self.assertIsNone(self.candidate())
        self.db.add(DispatchAudit(actor="test", action="followup_offsite_estimate", detail=str(order.id),
                                  created_at=self.now - timedelta(days=2)))
        self.assertIsNone(self.candidate())
        self.now += timedelta(hours=1)
        self.assertEqual(self.candidate()["reason"], "estimate")
        self.db.add(DispatchAudit(actor="test", action="followup_vehicle_present", detail=str(order.id)))
        self.db.flush()
        self.assertIsNone(self.candidate())

    def test_past_appointment_and_no_payment_need_a_recorded_no_show(self):
        order = self.order("intake")
        appointment = Appointment(repair_order_id=order.id, resource_id="fixture", status="confirmed",
                                  starts_at=self.now-timedelta(days=2, hours=1), ends_at=self.now-timedelta(days=2))
        self.db.add(appointment)
        self.sms("visit-arrangement", 80, "outbound", "See you at the shop")
        self.assertIsNone(self.candidate())
        self.db.flush()
        appointment.status = "cancelled"
        self.db.add(DispatchAudit(actor="test", action="followup_missed_visit", detail=str(appointment.id),
                                  created_at=self.now-timedelta(hours=25)))
        self.db.flush()
        self.assertEqual(self.candidate()["reason"], "missed_visit")
        self.db.add(Appointment(repair_order_id=order.id, resource_id="fixture", status="tentative",
                                starts_at=self.now+timedelta(days=1), ends_at=self.now+timedelta(days=1, hours=1)))
        self.db.flush()
        self.assertIsNone(self.candidate())
