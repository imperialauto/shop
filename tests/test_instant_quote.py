"""Instant website prices: the owner's price book, the price page and Hold this price. Made-up data, private database, nothing is sent."""
import json
import os
import socket
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app import instant_quote
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.public_site import MobileZone
from app.routes import instant_quote as quote_routes
from app.routes.invoices import totals_for

PASSWORD = "fixture-password-1"
ALTERNATOR = {"key": "alternator", "name": "Alternator replacement", "hours": 1.5, "labor": None, "parts": 250.0,
              "rate": "shop", "supplies": True, "mobile": True, "parts_words": ["alternator"], "note": "",
              "vehicles": [{"make": "Ford", "model": "F-150", "years": [2015, 2020], "hours": 2.1, "parts": None}]}


def no_network(*args, **kwargs):
    raise AssertionError("no outside calls in tests")


class InstantQuoteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.store_patch = patch.object(store, "SessionLocal", self.Session)
        self.store_patch.start()
        store.clear_cache()
        self.read_only = app.state.read_only
        app.state.read_only = False
        quote_routes.PRICE_LIMIT.seen.clear()
        quote_routes.HOLD_LIMIT.seen.clear()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            db.commit()

    def tearDown(self):
        self.store_patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def save_book(self, book):
        with self.Session() as db:
            store.save(db, instant_quote.SETTING, json.dumps(book), "owner")
            db.commit()
        store.clear_cache()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def counts(self):
        with self.Session() as db:
            return tuple(db.query(m).count() for m in (Customer, Vehicle, RepairOrder, LineItem, DispatchItem))

    def on(self, forms=True):
        return patch.dict(os.environ, {"INSTANT_QUOTES_ENABLED": "true", "PUBLIC_FORMS_ENABLED": "true" if forms else ""})

    # The price math

    def test_default_packages_match_the_published_prices(self):
        totals = {j["key"]: instant_quote.price(None, j, 2018, "Honda", "Civic") for j in instant_quote.jobs()}
        self.assertEqual(totals["oil-change"].subtotal, 79.99)
        self.assertEqual(totals["oil-change"].total, 86.24)  # tax on 67.94 of parts
        self.assertEqual(totals["brakes-front-sedan"].subtotal, 419.0)
        self.assertEqual(totals["brakes-front-suv"].subtotal, 379.0)
        self.assertEqual(totals["brakes-front-pickup"].subtotal, 439.0)
        self.assertEqual(totals["diagnostic"].total, 150.0)
        self.assertNotIn("Shop supplies", [line[0] for q in totals.values() for line in q.lines])

    def test_book_hours_times_the_rate_plus_parts_supplies_and_tax(self):
        quote = instant_quote.price(None, instant_quote.clean([ALTERNATOR])[0], 2012, "Toyota", "Camry")
        self.assertEqual(quote.lines[0][3], 225.0)  # 1.5 hr at $150
        self.assertEqual(quote.parts, 250.0)
        self.assertEqual([l[3] for l in quote.lines if l[0] == "Shop supplies"], [22.5])  # 10% of labor
        self.assertEqual(quote.tax, 23.0)  # 9.2% of parts only
        self.assertEqual(quote.total, 520.5)

    def test_vehicle_row_beats_book_hours_and_diesel_uses_the_diesel_rate(self):
        job = instant_quote.clean([ALTERNATOR])[0]
        truck = instant_quote.price(None, job, 2017, "ford", "f150")
        self.assertEqual((truck.hours, truck.rate, truck.lines[0][3]), (2.1, 150, 315.0))
        older = instant_quote.price(None, job, 2010, "Ford", "F-150")
        self.assertEqual(older.hours, 1.5)
        diesel = instant_quote.price(None, job, 2010, "Ford", "F-150", diesel=True)
        self.assertEqual((diesel.rate_kind, diesel.lines[0][3]), ("diesel", 262.5))

    def test_supplies_are_capped(self):
        job = instant_quote.clean([{**ALTERNATOR, "hours": 8.0, "vehicles": []}])[0]
        quote = instant_quote.price(None, job, 2012, "Ram", "2500")
        self.assertEqual([l[3] for l in quote.lines if l[0] == "Shop supplies"], [100.0])

    def test_a_job_without_numbers_is_not_priced(self):
        no_hours = instant_quote.clean([{**ALTERNATOR, "hours": None, "vehicles": []}])
        self.assertTrue(no_hours == [] or instant_quote.price(None, no_hours[0], 2012, "Kia", "Soul") is None)
        no_parts = instant_quote.clean([{**ALTERNATOR, "parts": None, "vehicles": []}])
        self.assertTrue(no_parts == [] or instant_quote.price(None, no_parts[0], 2012, "Kia", "Soul") is None)

    def test_onsite_needs_a_mobile_job_and_a_priced_zip(self):
        job = instant_quote.clean([ALTERNATOR])[0]
        self.assertIsNone(instant_quote.price(None, job, 2012, "Kia", "Soul", where="onsite", zone=None))
        self.assertIsNone(instant_quote.price(None, job, 2012, "Kia", "Soul", where="onsite", zone=MobileZone(None, 30)))
        onsite = instant_quote.price(None, job, 2012, "Kia", "Soul", where="onsite", zone=MobileZone(25.0, 30))
        self.assertEqual(onsite.lines[0][3], 247.5)  # 1.5 hr at the $165 mobile rate
        self.assertEqual([l[3] for l in onsite.lines if l[0] in ("Mobile service call", "Travel")], [75.0, 25.0])
        brakes = instant_quote.find("brakes-front-sedan")
        self.assertIsNone(instant_quote.price(None, brakes, 2012, "Kia", "Soul", where="onsite", zone=MobileZone(0, 30)))

    def test_parts_come_from_the_last_job_on_the_same_make_and_model(self):
        job = instant_quote.clean([{**ALTERNATOR, "vehicles": []}])[0]
        now = datetime.utcnow()
        with self.Session() as db:
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100"))
            db.add_all([Vehicle(id=1, customer_id=1, year=2014, make="Honda", model="Accord"),
                        Vehicle(id=2, customer_id=1, year=2005, make="Honda", model="Accord")])
            db.add_all([RepairOrder(id=7, customer_id=1, vehicle_id=1, status="closed", created_at=now - timedelta(days=40)),
                        RepairOrder(id=8, customer_id=1, vehicle_id=1, status="closed", created_at=now - timedelta(days=5)),
                        RepairOrder(id=9, customer_id=1, vehicle_id=2, status="closed", created_at=now - timedelta(days=1))])
            db.add_all([LineItem(repair_order_id=7, description="Reman alternator", item_type="part", quantity=1, unit_price=301.0),
                        LineItem(repair_order_id=8, description="New Alternator", item_type="part", quantity=1, unit_price=289.99),
                        LineItem(repair_order_id=8, description="Alternator belt", item_type="part", quantity=1, unit_price=30.0, approval="declined"),
                        LineItem(repair_order_id=9, description="Alternator", item_type="part", quantity=1, unit_price=99.0)])
            db.commit()
            self.assertEqual(instant_quote.last_paid(db, job, 2015, "Honda", "Accord"), (289.99, 8))
            quote = instant_quote.price(db, job, 2015, "Honda", "Accord")
            self.assertEqual(quote.parts, 289.99)
            self.assertIn("job #8", quote.parts_source)
            self.assertIsNone(instant_quote.last_paid(db, job, 2015, "Honda", "Civic"))

    def test_a_broken_saved_book_falls_back_to_the_defaults(self):
        with self.Session() as db:
            store.save(db, instant_quote.SETTING, "not json", "owner")
            db.commit()
        store.clear_cache()
        self.assertEqual([j["key"] for j in instant_quote.jobs()], [j["key"] for j in instant_quote.clean(instant_quote.DEFAULTS)])

    # The website pages

    def test_the_page_is_hidden_until_switched_on(self):
        client = TestClient(app)
        with patch.dict(os.environ, {"INSTANT_QUOTES_ENABLED": ""}):
            self.assertEqual(client.get("/site/quote").status_code, 404)
            self.assertEqual(client.post("/site/quote", data={"job": "oil-change"}).status_code, 404)
            self.assertEqual(client.post("/site/quote/hold", data={"job": "oil-change"}).status_code, 404)
        with self.on():
            page = client.get("/site/quote")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Get a price in seconds", page.text)

    def test_seeing_a_price_writes_nothing_and_sends_nothing(self):
        client = TestClient(app)
        with self.on(), patch.object(socket.socket, "connect", no_network):
            shown = client.post("/site/quote", data={"job": "brakes-front-sedan", "where": "shop",
                                                     "year": "2016", "make": "Toyota", "model": "Camry"})
        self.assertEqual(shown.status_code, 200)
        self.assertIn("$436.85", shown.text)  # $419 plus 9.2% of $194 parts
        self.assertIn("Hold this price", shown.text)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_unpriced_or_unserved_requests_point_to_an_estimate_request(self):
        client = TestClient(app)
        with self.on():
            bad = client.post("/site/quote", data={"job": "oil-change", "where": "onsite", "zip": "99999",
                                                   "year": "2016", "make": "Toyota", "model": "Camry"})
            self.assertEqual(bad.status_code, 422)
            self.assertIn('href="/site/estimate"', bad.text)
            self.assertEqual(client.post("/site/quote", data={"job": "nope", "year": "2016", "make": "Kia",
                                                              "model": "Soul"}).status_code, 422)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))

    def test_hold_makes_the_job_with_draft_lines_and_an_owner_item(self):
        client = TestClient(app, follow_redirects=False)
        data = {"job": "oil-change", "where": "shop", "year": "2016", "make": "Toyota", "model": "Camry",
                "first_name": "Testy", "last_name": "Sampleton", "phone": "(602) 555-0142", "contact_method": "text",
                "when": "Saturday morning"}
        with self.on(), patch.object(socket.socket, "connect", no_network):
            held = client.post("/site/quote/hold", data=data)
            self.assertEqual(held.status_code, 303)
            self.assertEqual(held.headers["location"], "/site/estimate/received")
            received = client.get("/site/estimate/received")
            self.assertIn("Price held: $86.24 with tax", received.text)
            again = client.post("/site/quote/hold", data=data)  # a double tap makes no second job
            self.assertEqual(again.status_code, 303)
        self.assertEqual(self.counts()[2], 1)
        with self.Session() as db:
            order = db.query(RepairOrder).one()
            lines = [(li.description, li.item_type, li.quantity, li.unit_price, li.approval) for li in order.line_items]
            self.assertEqual(lines, [("Full synthetic oil (per quart)", "part", 5.0, 10.99, None),
                                     ("Oil filter", "part", 1.0, 12.99, None),
                                     ("Oil change labor", "labor", 1.0, 12.05, None)])
            self.assertEqual(totals_for(order.line_items)[3], 86.24)
            self.assertIn("ALLDATA Shop Manager", order.concern)
            self.assertIn("Saturday morning", order.concern)
            item = db.query(DispatchItem).one()
            self.assertEqual(item.status, "needs_owner")
            self.assertIn("Instant price $86.24", item.summary)

    def test_hold_needs_public_forms_open_and_ignores_a_price_sent_by_the_browser(self):
        client = TestClient(app, follow_redirects=False)
        data = {"job": "diagnostic", "where": "shop", "year": "2016", "make": "Toyota", "model": "Camry",
                "first_name": "Testy", "last_name": "Sampleton", "phone": "(602) 555-0142", "contact_method": "text",
                "total": "1.00", "labor_0": "1"}
        with self.on(forms=False):
            self.assertEqual(client.post("/site/quote/hold", data=data).status_code, 503)
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0))
        with self.on():
            self.assertEqual(client.post("/site/quote/hold", data=data).status_code, 303)
        with self.Session() as db:
            self.assertEqual([li.unit_price for li in db.query(LineItem).all()], [150.0])

    # The owner's price book page

    def test_only_the_owner_can_open_the_price_book(self):
        self.assertEqual(self.sign_in("tech").get("/settings/instant-quotes").status_code, 403)
        page = self.sign_in().get("/settings/instant-quotes")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Instant price book", page.text)
        self.assertIn("/settings/instant-quotes", self.sign_in().get("/settings").text)

    def test_owner_adds_a_job_with_vehicle_rows_and_the_website_uses_it(self):
        client = self.sign_in()
        current = instant_quote.jobs()
        form = {}
        for i, job in enumerate(current):
            form.update({f"key_{i}": job["key"], f"name_{i}": job["name"],
                         f"labor_{i}": "" if job["labor"] is None else str(job["labor"]),
                         f"hours_{i}": "" if job["hours"] is None else str(job["hours"]),
                         f"parts_{i}": "" if job["parts"] is None else str(job["parts"]), f"rate_{i}": job["rate"],
                         f"note_{i}": job["note"], f"vehicles_{i}": instant_quote.vehicles_text(job),
                         f"words_{i}": ", ".join(job["parts_words"])})
        n = len(current)
        form.update({f"name_{n}": "Alternator replacement", f"hours_{n}": "1.5", f"parts_{n}": "250", f"rate_{n}": "shop",
                     f"supplies_{n}": "on", f"mobile_{n}": "on", f"words_{n}": "alternator",
                     f"vehicles_{n}": "Ford | F-150 | 2015-2020 | 2.1 |"})
        saved = client.post("/settings/instant-quotes", data=form)
        self.assertEqual(saved.status_code, 303, saved.text[:500])
        self.assertIn("saved=1", saved.headers["location"])
        added = next(j for j in instant_quote.jobs() if j["name"] == "Alternator replacement")
        self.assertEqual((added["hours"], added["supplies"], added["mobile"]), (1.5, True, True))
        self.assertEqual(instant_quote.price(None, added, 2017, "Ford", "F-150").hours, 2.1)
        with self.on():
            page = TestClient(app).get("/site/quote")
        self.assertIn("Alternator replacement", page.text)

    def test_bad_numbers_save_nothing_and_reset_restores_the_defaults(self):
        client = self.sign_in()
        bad = client.post("/settings/instant-quotes", data={"name_0": "Alternator", "hours_0": "lots", "parts_0": "250"})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("Nothing was saved", bad.text)
        self.save_book(instant_quote.clean([ALTERNATOR]))
        self.assertEqual([j["key"] for j in instant_quote.jobs()], ["alternator"])
        self.assertEqual(client.post("/settings/instant-quotes", data={"reset": "1"}).status_code, 303)
        self.assertIn("oil-change", [j["key"] for j in instant_quote.jobs()])


if __name__ == "__main__":
    unittest.main()
