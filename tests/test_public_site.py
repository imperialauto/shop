"""Public website pages and estimate requests. Made-up data only; nothing is sent to anyone."""
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
import socket
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import public_site
from app.appointment_model import Appointment
from app.database import Base, get_db
from app.main import app
from app.models import Communication, Customer, RepairOrder, Vehicle
from app.routes import public_site as public_routes

PAGES = ["/site", "/site/", "/site/services", "/site/contact", "/site/book", "/site/book?service=diagnostic",
         "/site/estimate", "/site/estimate?service=diesel", "/site/pricing", "/site/rentals", "/site/faq", "/site/status"] + [f"/site/services/{line.slug}" for line in public_site.SERVICE_LINES]


def no_network(*args, **kwargs):
    raise AssertionError("The public site must not contact anyone during tests.")


class InputRules(unittest.TestCase):
    def test_vin(self):
        self.assertEqual(public_site.normalize_vin(" 1ftfw1et5dfc10312 "), "1FTFW1ET5DFC10312")
        self.assertEqual(public_site.normalize_vin(""), "")
        for bad in ("1FTFW1ET5DFC1031", "1FTFW1ET5DFC1031O", "IFTFW1ET5DFC10312"):
            with self.assertRaises(public_site.FormError):
                public_site.normalize_vin(bad)

    def test_phone(self):
        self.assertEqual(public_site.normalize_phone("+1 (602) 555-0142"), "6025550142")
        for bad in ("555-0142", "1602555014222", "0025550142"):
            with self.assertRaises(public_site.FormError):
                public_site.normalize_phone(bad)
        self.assertEqual(public_site.phone_digits("1-602-555-0142"), "6025550142")

    def test_approved_services_come_from_setting(self):
        with patch.dict(os.environ, {"PUBLIC_BOOKING_SERVICES": "oil-service, nope"}):
            self.assertEqual([s.slug for s in public_site.approved_bookable_services()], ["oil-service"])
            self.assertIsNone(public_site.bookable_service("diagnostic"))
        with patch.dict(os.environ, {"PUBLIC_BOOKING_SERVICES": ""}):
            self.assertEqual(public_site.approved_bookable_services(), [])

    def test_forms_need_switch_and_writable_app(self):
        with patch.dict(os.environ, {"PUBLIC_FORMS_ENABLED": "true"}):
            self.assertTrue(public_site.forms_enabled(read_only=False))
            self.assertFalse(public_site.forms_enabled(read_only=True))
        with patch.dict(os.environ, {"PUBLIC_FORMS_ENABLED": ""}):
            self.assertFalse(public_site.forms_enabled(read_only=False))

    def test_limiter(self):
        limiter = public_site.SubmissionLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("a", 0))
        self.assertTrue(limiter.allow("a", 1))
        self.assertFalse(limiter.allow("a", 2))
        self.assertTrue(limiter.allow("b", 2))
        self.assertTrue(limiter.allow("a", 70))


class PublicPages(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            db = self.Session()
            try:
                yield db
            finally:
                db.close()
        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        public_routes.limiter.seen.clear()
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only
        self.client.close()

    def counts(self):
        with self.Session() as db:
            return tuple(db.query(model).count() for model in (Customer, Vehicle, RepairOrder, Appointment, Communication))

    def test_pages_render_without_sign_in_and_stay_out_of_search(self):
        for path in PAGES:
            response = self.client.get(path, headers={"accept": "text/html"})
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers.get("x-robots-tag"), "noindex, nofollow", path)
            self.assertIn('name="robots" content="noindex', response.text, path)
            self.assertIn("(480) 914-4144", response.text, path)
        self.assertEqual(self.client.get("/site/services/not-a-service").status_code, 404)
        self.assertEqual(self.client.get("/static/site.css").status_code, 200)

    def test_indexable_after_cutover_setting(self):
        with patch.dict(os.environ, {"PUBLIC_SITE_INDEXABLE": "true"}):
            response = self.client.get("/site/")
        self.assertNotIn("x-robots-tag", response.headers)
        self.assertNotIn('name="robots"', response.text)

    def test_staff_pages_still_require_sign_in(self):
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/login")
        self.assertEqual(self.client.get("/appointments/").status_code, 401)

    def test_forms_closed_while_read_only(self):
        app.state.read_only = True
        with patch.dict(os.environ, {"PUBLIC_FORMS_ENABLED": "true"}):
            page = self.client.get("/site/estimate")
            self.assertIn("open soon", page.text)
            self.assertNotIn('action="/site/estimate"', page.text)
            self.assertEqual(self.client.post("/site/estimate", data={"first_name": "Pat"}).status_code, 503)
            self.assertEqual(self.client.post("/site/book", data={"service": "diagnostic"}).status_code, 503)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_forms_closed_until_owner_switches_them_on(self):
        app.state.read_only = False
        with patch.dict(os.environ, {"PUBLIC_FORMS_ENABLED": ""}):
            self.assertEqual(self.client.post("/site/estimate", data=self.estimate()).status_code, 503)
            self.assertEqual(self.client.post("/site/book", data={"service": "diagnostic"}).status_code, 503)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    @staticmethod
    def estimate(**changes):
        data = {"first_name": "Testy", "last_name": "Sampleton", "phone": "(602) 555-0142", "email": "testy@example.com",
                "contact_method": "text", "service_line": "diesel", "where": "shop", "year": "2012", "make": "Ford",
                "model": "F-250", "vin": "1FTFW1ET5DFC10312", "mileage": "182,000",
                "description": "Made-up test: hard start when cold, white smoke for a minute."}
        return {**data, **changes}

    def open_forms(self):
        app.state.read_only = False
        return patch.dict(os.environ, {"PUBLIC_FORMS_ENABLED": "true"})

    def test_estimate_request_creates_intake_records_and_sends_nothing(self):
        # The test client runs in process, so any socket connection would be an outside call.
        with self.open_forms(), patch.object(socket.socket, "connect", no_network):
            response = self.client.post("/site/estimate", data=self.estimate(), follow_redirects=False)
            self.assertEqual(response.status_code, 303, response.text)
            received = self.client.get(response.headers["location"])
            self.assertIn("Reference WEB-", received.text)
            # The confirmation is shown once, then the page sends visitors back to the form.
            self.assertEqual(self.client.get("/site/estimate/received", follow_redirects=False).status_code, 303)
        self.assertEqual(self.counts(), (1, 1, 1, 0, 0))
        with self.Session() as db:
            order = db.query(RepairOrder).one()
            self.assertEqual(order.status, "intake")
            self.assertEqual(order.mileage_in, 182000)
            self.assertIn("Estimate request from the website.", order.concern)
            self.assertIn("Service: Diesel repair.", order.concern)
            self.assertIn("No message has been sent", order.concern)
            self.assertEqual(order.vehicle.vin, "1FTFW1ET5DFC10312")
            self.assertEqual(order.customer.phone, "(602) 555-0142")
            self.assertGreaterEqual(len(order.public_token), 32)

    def test_returning_customer_keeps_one_record(self):
        with self.open_forms():
            self.client.post("/site/estimate", data=self.estimate())
            self.client.post("/site/estimate", data=self.estimate(phone="602-555-0142", first_name="T", description="Made-up test: second visit for the same truck."))
            self.client.post("/site/estimate", data=self.estimate(last_name="Different", description="Made-up test: different person, same phone."))
        self.assertEqual(self.counts()[:3], (2, 2, 3))
        with self.Session() as db:
            second = db.get(RepairOrder, 2)
            self.assertEqual(second.customer_id, 1)
            self.assertEqual(second.vehicle_id, 1)  # same VIN on the same customer
            self.assertIn("Confirm before contacting", second.concern)

    def test_vin_alone_is_enough_for_an_estimate(self):
        with self.open_forms():
            response = self.client.post("/site/estimate", data=self.estimate(make="", model="", year=""), follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)

    def test_bad_input_is_explained_and_kept(self):
        with self.open_forms():
            cases = [
                (self.estimate(vin="NOTAVIN"), "17 letters"),
                (self.estimate(phone="12345"), "10-digit"),
                (self.estimate(description="bad"), "what is going on"),
                (self.estimate(where="roadside", location=""), "where the vehicle is"),
                (self.estimate(make="", vin=""), "make and model"),
                (self.estimate(contact_method="email", email=""), "email address"),
            ]
            for data, message in cases:
                response = self.client.post("/site/estimate", data=data)
                self.assertEqual(response.status_code, 422, message)
                self.assertIn(message, response.text)
                self.assertIn("Sampleton", response.text)  # what they typed is still there
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_spam_field_writes_nothing(self):
        with self.open_forms():
            response = self.client.post("/site/estimate", data=self.estimate(company="spam.example"), follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_submissions_are_limited_per_connection(self):
        with self.open_forms():
            codes = [self.client.post("/site/estimate", data=self.estimate(), follow_redirects=False).status_code for _ in range(6)]
        self.assertEqual(codes, [303] * 5 + [429])

    def test_another_site_cannot_post(self):
        with self.open_forms():
            response = self.client.post("/site/estimate", data=self.estimate(), headers={"origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_online_booking_refuses_without_postgresql(self):
        with self.open_forms(), patch.dict(os.environ, {"SHOP_HOURS_JSON": '{"0":["11:00","19:00"]}'}):
            page = self.client.get("/site/book?service=diagnostic")
            self.assertIn("not available right now", page.text)
            response = self.client.post("/site/book", data={**self.estimate(), "service": "diagnostic", "slot_id": "1"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
