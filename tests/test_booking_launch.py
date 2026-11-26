"""Website launch offer, booking confirmation texts and the website activity list.
Made-up data, a private in-memory database, nothing is sent to anyone."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import booking_confirmations, business_settings, launch_offer, website_alerts
from app.appointment_model import Appointment
from app.database import Base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Customer, RepairOrder, Vehicle


def settings(**values):
    """Patch business settings with made-up values; everything else reads as unset."""
    return patch.object(business_settings, "raw", lambda key: values.get(key))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def job(self, db, phone="602-555-0177", concern="Online booking: Basic in-shop diagnostic screening on Tue.", last="Sampleton"):
        customer = Customer(first_name="Testy", last_name=last, phone=phone)
        db.add(customer)
        db.flush()
        vehicle = Vehicle(customer_id=customer.id, make="Ford", model="Focus", year=2016)
        db.add(vehicle)
        db.flush()
        order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="intake", concern=concern,
                            public_token="fixture-token-" + "x" * 24 + str(customer.id))
        db.add(order)
        db.flush()
        return order

    def booking(self, db, order, days=3, status="confirmed", location="shop"):
        start = datetime.now(timezone.utc) + timedelta(days=days)
        visit = Appointment(repair_order_id=order.id, resource_id="bay-1", starts_at=start, ends_at=start + timedelta(hours=1),
                            status=status, location=location)
        db.add(visit)
        db.flush()
        item = website_alerts.queue(db, order, "booking", "Booked a made-up diagnostic.", vehicle_text="2016 Ford Focus")
        return visit, item


class LaunchOfferTests(Fixture):
    def test_on_by_default_and_can_be_turned_off(self):
        with settings():
            self.assertEqual(launch_offer.config(), {"on": True, "percent": 20, "limit": 3})
        with settings(launch_offer='{"on": false}'):
            self.assertFalse(launch_offer.config()["on"])
        with settings(launch_offer='{"on": true, "percent": 90, "limit": 3}'):
            self.assertFalse(launch_offer.config()["on"])  # a typo can't give away the shop
        with settings(launch_offer="not json"):
            self.assertTrue(launch_offer.config()["on"])

    def test_first_three_customers_one_each(self):
        with settings(), self.Session() as db:
            lines = []
            for n, phone in enumerate(["602-555-0101", "602-555-0101", "602-555-0102", "602-555-0103", "602-555-0104"]):
                line = launch_offer.claim(db, phone)
                lines.append(line)
                self.job(db, phone=phone, concern="Estimate request from the website.\n" + line, last=f"Fixture{n}")
            self.assertIn("20% off labor (online customer 1 of 3)", lines[0])
            self.assertEqual(lines[1], "")  # same phone, already has it
            self.assertIn("online customer 2 of 3", lines[2])
            self.assertIn("online customer 3 of 3", lines[3])
            self.assertEqual(lines[4], "")  # all three spots taken
            self.assertEqual(launch_offer.spots_left(db), 0)
        with settings(launch_offer='{"on": false}'), self.Session() as db:
            self.assertEqual(launch_offer.claim(db, "602-555-0199"), "")

    def test_banner_shows_on_site_pages_only_while_spots_are_left(self):
        client = TestClient(app)
        with patch.object(launch_offer, "banner", lambda: {"percent": 20, "limit": 3, "left": 2}):
            page = client.get("/site/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("20% off labor for our first 3 online bookings", page.text)
            self.assertIn("2 left. Labor only, one per customer.", page.text)
        with patch.object(launch_offer, "banner", lambda: None):
            self.assertNotIn("Launch offer:", client.get("/site/").text)
        client.close()


class ConfirmationTextTests(Fixture):
    def test_off_by_default_and_only_for_text(self):
        with self.Session() as db:
            order = self.job(db)
            visit, item = self.booking(db, order)
            with settings():
                self.assertFalse(booking_confirmations.queue(db, visit, item, "text"))
            with settings(booking_confirmation_texts="true"):
                self.assertFalse(booking_confirmations.queue(db, visit, item, "call"))
                self.assertFalse(booking_confirmations.queue(db, visit, item, "email"))
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_queued_once_and_handed_out_with_current_details(self):
        with settings(booking_confirmation_texts="true"), self.Session() as db:
            order = self.job(db, concern="Online booking: Basic in-shop diagnostic screening on Tue.\n"
                                         "Launch offer: 20% off labor (online customer 1 of 3).")
            visit, item = self.booking(db, order)
            self.assertTrue(booking_confirmations.queue(db, visit, item, "text"))
            db.commit()
            out = booking_confirmations.due(db)
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["phone"], "+16025550177")
            text = out[0]["text"]
            self.assertTrue(text.startswith("Hi Testy, you're booked at Imperial Auto Care for Basic in-shop diagnostic screening on "))
            self.assertIn(f"Your reference is WEB-{order.id}.", text)
            self.assertIn("7915 N Glen Harbor Blvd, Suite 508, Glendale", text)
            self.assertIn("launch offer: 20% off labor", text)
            self.assertIn("https://imperialautophx.com/status/fixture-token-", text)
            self.assertNotIn("railway", text)
            self.assertTrue(text.endswith("Reply STOP to opt out. Jae"))
            self.assertEqual(booking_confirmations.due(db), [])  # handed out once, never again

    def test_skipped_when_cancelled_past_or_switched_off(self):
        with settings(booking_confirmation_texts="true"), self.Session() as db:
            cancelled = self.booking(db, self.job(db, phone="602-555-0111", last="One"), status="cancelled")
            past = self.booking(db, self.job(db, phone="602-555-0112", last="Two"), days=-1)
            for visit, item in (cancelled, past):
                self.assertTrue(booking_confirmations.queue(db, visit, item, "text"))
            db.commit()
            self.assertEqual(booking_confirmations.due(db), [])
            self.assertEqual({r.status for r in db.query(DispatchOutbound).all()}, {"skipped"})
        with self.Session() as db:
            order = self.job(db, phone="602-555-0113", last="Three")
            visit, item = self.booking(db, order)
            with settings(booking_confirmation_texts="true"):
                booking_confirmations.queue(db, visit, item, "text")
                db.commit()
            with settings():
                self.assertEqual(booking_confirmations.due(db), [])

    def test_mobile_visit_wording(self):
        with self.Session() as db:
            order = self.job(db)
            visit, _ = self.booking(db, order, location="mobile")
            self.assertIn("We'll come to the address you gave.", booking_confirmations.message_for(order, visit))


class WebsiteActivityTests(Fixture):
    def test_lists_bookings_and_requests_newest_first(self):
        with self.Session() as db:
            booked = self.job(db, concern="Online booking: Basic in-shop diagnostic screening on Tue.\n"
                                          "Launch offer: 20% off labor (online customer 1 of 3).")
            visit, _ = self.booking(db, booked)
            tow = self.job(db, phone="602-555-0144", concern="Roadside request from the website.", last="Visitor")
            website_alerts.queue(db, tow, "request", "Made-up tow on the shoulder.", service_line="towing")
            db.commit()
            listed = website_alerts.recent(db)
            self.assertEqual(listed["count"], 2)
            kinds = {i["ref"]: i for i in listed["items"]}
            self.assertEqual(kinds[f"WEB-{tow.id}"]["kind"], "roadside request")
            self.assertTrue(kinds[f"WEB-{tow.id}"]["urgent"])
            self.assertTrue(kinds[f"WEB-{tow.id}"]["needs_owner"])
            entry = kinds[f"WEB-{booked.id}"]
            self.assertEqual((entry["kind"], entry["visit_status"], entry["confirmation_text"]), ("booking", "confirmed", "none"))
            self.assertTrue(entry["launch_offer"])
            self.assertEqual(entry["customer"], "Testy Sampleton")
            later = website_alerts.recent(db, since=datetime.now(timezone.utc) + timedelta(minutes=1))
            self.assertEqual(later["count"], 0)

    def test_connector_offers_it_read_only(self):
        from app import agent_connector
        scope, _, _, _, read_only = agent_connector.TOOLS["list_website_activity"]
        self.assertEqual((scope, read_only), ("appointments:read", True))


if __name__ == "__main__":
    unittest.main()
