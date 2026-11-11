"""Public website pages and estimate requests. Made-up data only; nothing is sent to anyone."""
import json
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
        with patch.dict(os.environ, {"PUBLIC_BOOKING_SERVICES": "ac-check, oil-service, nope"}):
            self.assertEqual([s.slug for s in public_site.approved_bookable_services()], ["ac-check"])
            self.assertIsNone(public_site.bookable_service("diagnostic"))
        with patch.dict(os.environ, {"PUBLIC_BOOKING_SERVICES": ""}):
            self.assertEqual(public_site.approved_bookable_services(), [])

    def test_package_tiers_are_starting_prices_and_unpriced_services_are_requestable(self):
        from app import business_settings
        catalog = {"diagnostic": {"price": 75}, "brake-pads-rotors": {"tiers": {"sedan": 349, "suv": "399", "boat": 1, "pickup": 449}}}
        with patch.dict(os.environ, {"PUBLIC_SERVICE_CATALOG": json.dumps(catalog)}), patch.object(business_settings, "_load", return_value={}):
            brakes = public_site.bookable_service("brake-pads-rotors")
            self.assertEqual(brakes.price_label, "From $349 per axle")
            self.assertEqual([t[0] for t in brakes.tiers], ["sedan", "suv", "pickup"])  # unknown classes dropped
            self.assertEqual(brakes.tier("suv")[2], 399.0)
            self.assertEqual(public_site.bookable_service("diagnostic").price_label, "$75")
            self.assertIn("ac-check", [s.slug for s in public_site.requestable_services()])
            self.assertNotIn("diagnostic", [s.slug for s in public_site.requestable_services()])

    def test_auto_times_setting(self):
        from app import business_settings
        cases = [('{"resources": ["bay-1"], "step_minutes": 30}', {"resources": ["bay-1"], "step_minutes": 30}),
                 ('{"resources": ["bay-1"], "step_minutes": 7}', {}), ('{"resources": []}', {}), ("nonsense", {}), ("", {})]
        for raw, expected in cases:
            with patch.dict(os.environ, {"PUBLIC_AUTO_TIMES": raw}), patch.object(business_settings, "_load", return_value={}):
                self.assertEqual(public_site.auto_times(), expected, raw)

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
        public_routes.STATUS_LIMIT.seen.clear()
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

    def test_every_service_leads_to_an_online_action(self):
        import re
        for line in public_site.SERVICE_LINES:
            page = self.client.get(f"/site/services/{line.slug}").text
            hero = page.split('class="hero"')[1].split("</section>")[0]
            buttons = re.findall(r'<a class="btn[^"]*" href="([^"]+)"', hero)
            self.assertTrue(buttons and buttons[0].startswith("/site/"), (line.slug, buttons))  # main action is online
            phones = [b for b in buttons if b.startswith("tel:")]
            self.assertEqual(bool(phones), line.slug in public_site.ROADSIDE_LINES, line.slug)  # phone only as emergency
        home = self.client.get("/site/").text
        self.assertIn('href="/site/roadside"', home)

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
            self.assertIn("Your reference", received.text)
            self.assertIn("WEB-1", received.text)
            # A refresh keeps the reference on screen in the same browser.
            again = self.client.get("/site/estimate/received", follow_redirects=False)
            self.assertEqual(again.status_code, 200)
            self.assertIn("not a booking yet", again.text)
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
            codes = [self.client.post("/site/estimate", data=self.estimate(description=f"Made-up test number {n}: rough idle."),
                                      follow_redirects=False).status_code for n in range(6)]
        self.assertEqual(codes, [303] * 5 + [429])

    def test_estimate_starts_from_approved_prices_only(self):
        from app import business_settings
        from app.dispatch_model import DispatchItem
        from app.models import LineItem
        catalog = json.dumps({"diagnostic": {"price": 75}})
        with self.open_forms(), patch.dict(os.environ, {"PUBLIC_SERVICE_CATALOG": catalog}), \
             patch.object(business_settings, "_load", return_value={}), patch.object(socket.socket, "connect", no_network):
            self.client.post("/site/estimate", data=self.estimate())  # diesel, at the shop
            self.client.post("/site/estimate", data=self.estimate(service_line="auto-repair", where="onsite", location="Made-up 1 Test St",
                                                                   description="Made-up test: no start at home."))
        with self.Session() as db:
            first, second = db.get(RepairOrder, 1), db.get(RepairOrder, 2)
            lines = db.query(LineItem).filter(LineItem.repair_order_id == 1).all()
            self.assertEqual([(l.unit_price, l.approval) for l in lines], [(75.0, None)])  # added, not sent
            self.assertIn("Labor rate: $175/hr (diesel)", first.concern)
            self.assertEqual(db.query(LineItem).filter(LineItem.repair_order_id == 2).count(), 0)  # on-site: no guessed diagnostic
            self.assertIn("$165/hr (mobile)", second.concern)
            self.assertIn("travel not set", second.concern)
            item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == "WEB1").one()
            self.assertIn("ALLDATA", item.summary)
        self.assertEqual(self.counts()[4], 0)  # no message to the customer

    def roadside(self, **changes):
        data = {"situation": "tow", "location": "Made-up: I-17 north at mile 210", "first_name": "Road", "last_name": "Tester",
                "phone": "(602) 555-0150", "contact_method": "call", "make": "Ford", "model": "F-150", "description": "Made-up: needs the shop."}
        return self.client.post("/site/roadside", data={**data, **changes}, follow_redirects=False)

    def test_roadside_request_is_written_urgent_and_never_dispatched(self):
        from app.dispatch_model import DispatchItem
        from app.models import LineItem
        self.assertIn("call 911 first", self.client.get("/site/roadside").text)
        with self.open_forms(), patch.object(socket.socket, "connect", no_network):
            for bad, message in (({"situation": ""}, "Choose what"), ({"location": "x"}, "where the vehicle is"),
                                 ({"make": "", "model": ""}, "make and model")):
                response = self.roadside(**bad)
                self.assertEqual(response.status_code, 422)
                self.assertIn(message, response.text)
            done = self.roadside()
            self.assertEqual(done.status_code, 303)
            page = self.client.get(done.headers["location"]).text
            self.assertIn("Nothing is dispatched until we call you", page)
        with self.Session() as db:
            order = db.query(RepairOrder).one()
            self.assertIn("Not dispatched", order.concern)
            self.assertIn("I-17 north at mile 210", order.concern)
            item = db.query(DispatchItem).one()
            self.assertEqual((item.urgency, item.status, item.draft_reply), ("urgent", "needs_owner", None))
            self.assertTrue(item.quo_conversation_id.startswith("WEB"))  # website no-text guards apply
            self.assertEqual(db.query(LineItem).count(), 0)  # towing is a custom quote
        self.assertEqual(self.counts()[4], 0)

    def test_roadside_closed_with_forms(self):
        app.state.read_only = False
        self.assertEqual(self.roadside().status_code, 503)

    def test_sending_the_same_request_again_keeps_one_job(self):
        with self.open_forms():
            first = self.client.post("/site/estimate", data=self.estimate(), follow_redirects=False)
            ref = self.client.get(first.headers["location"]).text.split("WEB-")[1][:3]
            again = self.client.post("/site/estimate", data=self.estimate(description="  made-up test: hard start when cold,  white smoke for a minute. "), follow_redirects=False)
            self.assertEqual(again.status_code, 303)
            self.assertIn("WEB-" + ref, self.client.get(again.headers["location"]).text)
        self.assertEqual(self.counts()[2], 1)

    def lost(self, **data):
        return self.client.post("/site/status/lost", data={"phone": "(602) 555-0142", "last_name": "Sampleton", **data})

    def test_lost_reference_queues_help_without_revealing_or_booking(self):
        from app.dispatch_model import DispatchItem
        with self.open_forms(), patch.object(socket.socket, "connect", no_network):
            self.client.post("/site/estimate", data=self.estimate())
            before = self.counts()
            hit, miss, wrong_name = self.lost(), self.lost(phone="(602) 555-0199"), self.lost(last_name="Nobody")
            again = self.lost()
        for page in (hit, miss, wrong_name, again):
            self.assertEqual(page.status_code, 200)
            self.assertIn("If that matches a booking or request", page.text)
            self.assertNotIn("WEB-", page.text.split("Lost your reference")[1])  # nothing about the record is shown
        self.assertEqual(self.counts(), before)  # no booking, customer or message created
        with self.Session() as db:
            items = db.query(DispatchItem).filter(DispatchItem.category == "reference_help").all()
            self.assertEqual(len(items), 1)  # asking twice refreshes the same item
            self.assertTrue(items[0].quo_conversation_id.startswith("WEB"))  # website no-text guards apply
            self.assertIn("WEB-1", items[0].summary)
            self.assertIsNone(items[0].draft_reply)

    def test_lost_reference_closed_with_forms(self):
        app.state.read_only = False
        self.assertEqual(self.lost().status_code, 503)

    def test_status_page_is_private_and_shows_no_internal_notes(self):
        with self.open_forms():
            self.client.post("/site/estimate", data=self.estimate())
            self.client.post("/site/estimate", data=self.estimate(description="Made-up test: second visit, same truck."))
        with self.Session() as db:
            token = db.get(RepairOrder, 2).public_token
        page = self.client.get(f"/status/{token}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("noindex", page.headers["x-robots-tag"])
        self.assertIn("WEB-2", page.text)
        self.assertIn("not booked yet", page.text)
        for internal in ("Confirm before contacting", "No message has been sent", "Preferred contact"):
            self.assertNotIn(internal, page.text)
        self.assertEqual(self.client.get(f"/status/{token}/visit.ics").status_code, 404)  # nothing booked
        self.assertEqual(self.client.get("/status/not-a-token").status_code, 404)

    def test_booked_visit_shows_on_status_page_and_calendar_file(self):
        from datetime import datetime, timedelta, timezone
        with self.open_forms():
            self.client.post("/site/estimate", data=self.estimate())
        with self.Session() as db:
            order = db.get(RepairOrder, 1)
            start = datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc)
            db.add(Appointment(repair_order_id=1, resource_id="bay-1", starts_at=start, ends_at=start + timedelta(hours=1),
                               status="confirmed", location="shop"))
            db.commit()
            token = order.public_token
        page = self.client.get(f"/status/{token}")
        self.assertIn("Booked", page.text)
        self.assertIn("Dec 1, 11:00 AM", page.text)
        ics = self.client.get(f"/status/{token}/visit.ics")
        self.assertEqual(ics.status_code, 200)
        self.assertIn("text/calendar", ics.headers["content-type"])
        self.assertIn("DTSTART:20261201T180000Z", ics.text)
        self.assertIn("WEB-1", ics.text)
        self.assertIn(f"/status/{token}", ics.text)

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


class PreviewGateTests(unittest.TestCase):
    ENV = {"PUBLIC_PREVIEW_MODE": "true", "PREVIEW_ACCESS_CODE": "made-up-code", "RAILWAY_ENVIRONMENT_NAME": "preview", "RAILWAY_PROJECT_ID": "other"}

    def test_gate_only_runs_in_preview(self):
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.get("/site/").status_code, 200)
        with patch.dict(os.environ, {**self.ENV, "RAILWAY_ENVIRONMENT_NAME": "production"}):
            self.assertEqual(client.get("/site/").status_code, 200)

    def test_code_is_required_in_preview(self):
        client = TestClient(app, follow_redirects=False, base_url="https://testserver")
        with patch.dict(os.environ, self.ENV):
            self.assertEqual(client.get("/site/").headers["location"], "/preview-access")
            self.assertEqual(client.get("/healthz").status_code, 200)
            self.assertEqual(client.post("/preview-access", data={"code": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/preview-access", data={"code": "made-up-code"}).status_code, 303)
            page = client.get("/site/")
            self.assertEqual(page.status_code, 200)
            self.assertIn("noindex", page.headers["x-robots-tag"])


if __name__ == "__main__":
    unittest.main()
