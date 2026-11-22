"""A contact renamed in Quo renames the matching customer. Made-up data, private database, no live services."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import quo_contact_names
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, User
from app.routes import internal_messaging

KEY = "fixture-internal-key"
HEADERS = {"X-Internal-Key": KEY}
PASSWORD = "fixture-password-1"


class QuoContactNameTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        p = patch.object(internal_messaging, "INTERNAL_API_KEY", KEY)
        p.start()
        self.addCleanup(p.stop)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add_all([Customer(id=1, first_name="Unknown", last_name="(0111)", phone="+16025550111"),
                        Customer(id=2, first_name="Honda", last_name="Accord", phone="(602) 555-0122", name_source="quo"),
                        Customer(id=3, first_name="Robin", last_name="Typed", phone="6025550133", name_source="staff")])
            db.commit()
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def post(self, first, last="", phone="+16025550111", company="", key=KEY):
        return self.client.post("/internal/dispatch/contact", headers={"X-Internal-Key": key},
                                json={"id": "CT1", "firstName": first, "lastName": last, "company": company, "phones": [phone]})

    def customer(self, cid):
        with self.Session() as db:
            c = db.get(Customer, cid)
            return c.first_name, c.last_name, c.name_source

    def test_wrong_key_is_refused(self):
        self.assertIn(self.post("Dana", "Sample", key="wrong").status_code, (401, 403))
        self.assertEqual(self.customer(1)[0], "Unknown")

    def test_a_real_name_renames_the_customer(self):
        self.assertEqual(self.post("Dana", "Sample").json(), {"updated": 1, "emails": 0, "reason": None})
        self.assertEqual(self.customer(1), ("Dana", "Sample", "quo_contact"))

    def test_a_job_label_replaces_an_old_job_label_never(self):
        self.assertEqual(self.post("Honda Accord", "Oil Leak", phone="+16025550122").json()["reason"], "not_a_name")
        self.assertEqual(self.post("New Lead", "0111").json()["reason"], "not_a_name")
        self.assertEqual(self.post("2014 Sprinter", "Diag").json()["reason"], "not_a_name")
        self.assertEqual(self.customer(1)[0], "Unknown")

    def test_a_job_label_name_gets_fixed_by_a_real_name(self):
        self.post("Amaya", "Lopez", phone="+16025550122")
        self.assertEqual(self.customer(2), ("Amaya", "Lopez", "quo_contact"))

    def test_a_name_typed_in_the_dashboard_wins(self):
        self.assertEqual(self.post("Someone", "Else", phone="+16025550133").json()["reason"], "typed_in_dashboard")
        self.assertEqual(self.customer(3)[:2], ("Robin", "Typed"))

    def test_no_customer_means_nothing_is_created(self):
        self.assertEqual(self.post("Dana", "Sample", phone="+16025550199").json()["reason"], "no_customer")
        with self.Session() as db:
            self.assertEqual(db.query(Customer).count(), 3)

    def test_same_name_is_left_alone(self):
        self.post("Dana", "Sample")
        self.assertEqual(self.post("Dana", "Sample").json()["reason"], "same")

    def test_business_name_and_single_names(self):
        self.assertEqual(quo_contact_names.person_name("", "", "Sample Fleet LLC"), ("Sample Fleet LLC", ""))
        self.assertEqual(quo_contact_names.person_name("Amaya", "-", ""), None)
        self.assertEqual(quo_contact_names.person_name("Amaya", "", ""), ("Amaya", ""))

    def test_editing_the_name_in_the_dashboard_marks_it_typed(self):
        owner = TestClient(app, follow_redirects=False)
        owner.post("/login", data={"username": "owner", "password": PASSWORD})
        owner.post("/customers/1/edit", data={"first_name": "Jordan", "last_name": "Sample", "phone": "+16025550111"})
        self.assertEqual(self.customer(1), ("Jordan", "Sample", "staff"))
        self.assertEqual(self.post("Dana", "Sample").json()["reason"], "typed_in_dashboard")

    def test_an_empty_email_is_filled_and_a_typed_one_kept(self):
        self.client.post("/internal/dispatch/contact", headers=HEADERS,
                         json={"id": "CT1", "firstName": "Dana", "lastName": "Sample", "emails": ["dana@example.test"], "phones": ["+16025550111"]})
        with self.Session() as db:
            self.assertEqual(db.get(Customer, 1).email, "dana@example.test")
            db.get(Customer, 3).email = "robin@example.test"
            db.commit()
        self.client.post("/internal/dispatch/contact", headers=HEADERS,
                         json={"id": "CT3", "firstName": "Robin", "emails": ["other@example.test"], "phones": ["6025550133"]})
        with self.Session() as db:
            self.assertEqual(db.get(Customer, 3).email, "robin@example.test")

    def test_hourly_catch_up_renames_and_shows_on_home_health(self):
        from app import system_health
        from app.dispatch_model import DispatchSync
        with self.Session() as db:
            self.assertNotIn("Quo contact names", [c["name"] for c in system_health.report(db)["checks"]])
        r = self.client.post("/internal/dispatch/contacts", headers=HEADERS, json={"contacts": [
            {"id": "A", "firstName": "Dana", "lastName": "Sample", "phones": ["+16025550111"]},
            {"id": "B", "firstName": "Toyota", "lastName": "Camry", "phones": ["+16025550122"]},
            {"id": "C", "firstName": "Nobody", "phones": ["+16025550199"]}]})
        self.assertEqual(r.json(), {"contacts": 3, "renamed": 1, "emails": 0})
        self.assertEqual(self.customer(1), ("Dana", "Sample", "quo_contact"))
        with self.Session() as db:
            line = [c for c in system_health.report(db)["checks"] if c["name"] == "Quo contact names"][0]
            self.assertTrue(line["ok"])
            self.assertIn("renamed 1", line["detail"])
            from datetime import datetime, timedelta, timezone
            db.get(DispatchSync, "quo_contacts").last_success_at = datetime.now(timezone.utc) - timedelta(hours=4)
            db.commit()
            line = [c for c in system_health.report(db)["checks"] if c["name"] == "Quo contact names"][0]
            self.assertFalse(line["ok"])
        self.assertEqual(self.client.post("/internal/dispatch/contacts", headers={"X-Internal-Key": "wrong"},
                                          json={"contacts": []}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
