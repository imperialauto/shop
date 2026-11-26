"""Canned jobs: one tap on the job page adds a package's lines. Made-up data, private database, nothing is sent."""
import json
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app import canned_jobs
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle
from app.routes.invoices import totals_for

PASSWORD = "fixture-password-1"


class CannedJobTests(unittest.TestCase):
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
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100"))
            db.add(Vehicle(id=1, customer_id=1, year=2015, make="Ford", model="Edge"))
            db.add(RepairOrder(id=5, customer_id=1, vehicle_id=1, status="intake"))
            db.commit()

    def tearDown(self):
        self.store_patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def lines(self):
        with self.Session() as db:
            return [(li.description, li.item_type, li.quantity, li.unit_price, li.approval)
                    for li in db.query(LineItem).order_by(LineItem.id).all()]

    def test_default_totals(self):
        by_key = {p["key"]: canned_jobs.totals(p) for p in canned_jobs.packages()}
        self.assertEqual(by_key["oil-change"], {"subtotal": 79.99, "tax": 6.25, "total": 86.24})  # tax on 67.94 of parts
        self.assertEqual(by_key["brakes-front-sedan"]["subtotal"], 419.0)
        self.assertEqual(by_key["brakes-front-suv"]["subtotal"], 379.0)
        self.assertEqual(by_key["brakes-front-pickup"]["subtotal"], 439.0)
        self.assertEqual(by_key["brakes-front-sedan"]["tax"], 17.85)  # $194 parts at 9.2%
        self.assertEqual(by_key["diagnostic"], {"subtotal": 150.0, "tax": 0.0, "total": 150.0})

    def test_one_tap_adds_the_oil_change_as_draft_lines_with_the_right_total(self):
        client = self.sign_in("tech")  # anyone who can add a line can add a package
        self.assertIn("Add a package", client.get("/ro/5").text)
        done = client.post("/ro/5/add-package", data={"package": "oil-change"})
        self.assertEqual(done.status_code, 303)
        self.assertIn("package_added=1", done.headers["location"])
        self.assertEqual(self.lines(), [("Full synthetic oil (per quart)", "part", 5.0, 10.99, None),
                                        ("Oil filter", "part", 1.0, 12.99, None),
                                        ("Oil change labor", "labor", 1.0, 12.05, None)])
        with self.Session() as db:
            ro = db.get(RepairOrder, 5)
            subtotal, _rate, tax, total = totals_for(ro.line_items)
            self.assertEqual((round(subtotal, 2), tax, total), (79.99, 6.25, 86.24))
            self.assertEqual((db.query(DispatchItem).count(), db.query(DispatchOutbound).count()), (0, 0))

    def test_brake_package_and_unknown_package(self):
        client = self.sign_in()
        client.post("/ro/5/add-package", data={"package": "brakes-front-suv"})
        self.assertEqual(self.lines(), [("Front brake pads and rotors labor", "labor", 1.5, 150.0, None),
                                        ("Front brake pads and rotors (parts)", "part", 1.0, 154.0, None)])
        bad = client.post("/ro/5/add-package", data={"package": "nope"})
        self.assertIn("package_error=1", bad.headers["location"])
        self.assertEqual(len(self.lines()), 2)

    def test_never_onto_a_sent_invoice(self):
        with self.Session() as db:
            db.add(Invoice(id=9, repair_order_id=5, status="sent", total=0))
            db.commit()
        done = self.sign_in().post("/ro/5/add-package", data={"package": "diagnostic"})
        self.assertIn("line_error=invoiced", done.headers["location"])
        self.assertEqual(self.lines(), [])

    def test_owner_edits_packages_and_they_are_saved(self):
        client = self.sign_in()
        page = client.get("/settings/canned-jobs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Oil change special", page.text)
        done = client.post("/settings/canned-jobs", data={
            "key_0": "oil-change", "name_0": "Oil change special",
            "lines_0": "Full synthetic oil (per quart) | part | 6 | 10.99\nOil filter | part | 1 | $12.99",
            "key_1": "diagnostic", "name_1": "Diagnostic", "lines_1": "Diag | labor | 1 | 150", "remove_1": "on",
            "name_2": "Rear brake job, sedan", "lines_2": "Rear brakes labor | labor | 1.5 | 150"})
        self.assertEqual(done.status_code, 303)
        self.assertIn("saved=1", done.headers["location"])
        store.clear_cache()
        with self.Session() as db:
            saved = json.loads(db.get(store.BusinessSetting, "canned_jobs").value)
            self.assertEqual(db.get(store.BusinessSetting, "canned_jobs").updated_by, "owner")
        self.assertEqual([p["name"] for p in saved], ["Oil change special", "Rear brake job, sedan"])
        self.assertEqual(saved[0]["lines"][0]["quantity"], 6.0)
        self.assertTrue(saved[0]["lines"][0]["taxable"])
        self.assertEqual([p["key"] for p in canned_jobs.packages()], ["oil-change", "rear-brake-job-sedan"])
        # A bad row is refused and nothing changes.
        bad = client.post("/settings/canned-jobs", data={"name_0": "Broken", "lines_0": "No price here | labor | 1"})
        self.assertEqual(bad.status_code, 422)
        store.clear_cache()
        self.assertEqual(len(canned_jobs.packages()), 2)
        # Back to defaults.
        client.post("/settings/canned-jobs", data={"reset": "1"})
        store.clear_cache()
        self.assertEqual(len(canned_jobs.packages()), 5)

    def test_only_the_owner_edits_packages(self):
        client = self.sign_in("tech")
        self.assertEqual(client.get("/settings/canned-jobs").status_code, 403)
        self.assertEqual(client.post("/settings/canned-jobs", data={"reset": "1"}).status_code, 403)

    def test_bad_saved_value_falls_back_to_defaults(self):
        with patch.object(store, "raw", lambda key: "not json"):
            self.assertEqual(len(canned_jobs.packages()), 5)


if __name__ == "__main__":
    unittest.main()
