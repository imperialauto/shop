"""Customer, vehicle and job from one form; no duplicate customers by phone. Made-up data."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"


class FastIntakeTests(unittest.TestCase):
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
        with self.Session() as db:
            db.add(User(id=1, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"))
            db.commit()
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"username": "office", "password": PASSWORD})

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def counts(self):
        with self.Session() as db:
            return db.query(Customer).count(), db.query(Vehicle).count(), db.query(RepairOrder).count()

    def test_one_form_makes_customer_vehicle_and_job(self):
        r = self.client.post("/customers/new", data={"first_name": "Sam", "last_name": "Rivera", "phone": "623-555-0177",
                                                     "vin": "1fadp3e25gl254455", "year": "2016", "make": "Ford", "model": "Focus",
                                                     "concern": "Clutch shudder", "then": "job"})
        self.assertEqual(self.counts(), (2, 1, 1))
        with self.Session() as db:
            job = db.query(RepairOrder).one()
            self.assertEqual(r.headers["location"], f"/ro/{job.id}")
            self.assertEqual((job.concern, job.vehicle.vin, job.vehicle.year, job.customer.first_name),
                             ("Clutch shudder", "1FADP3E25GL254455", 2016, "Sam"))
            self.assertTrue(job.public_token)

    def test_job_without_vehicle_opens_the_job_form(self):
        r = self.client.post("/customers/new", data={"first_name": "Lee", "last_name": "Other", "then": "job"})
        self.assertTrue(r.headers["location"].startswith("/ro/new?customer_id="))
        self.assertEqual(self.counts(), (2, 0, 0))

    def test_customer_only(self):
        r = self.client.post("/customers/new", data={"first_name": "Lee", "last_name": "Other", "then": "customer"})
        self.assertRegex(r.headers["location"], r"^/customers/\d+$")

    def test_known_phone_opens_the_existing_file(self):
        r = self.client.post("/customers/new", data={"first_name": "Patricia", "last_name": "S", "phone": "+1 602.555.0100",
                                                     "make": "Ford", "model": "F-250", "then": "job"})
        self.assertEqual(r.headers["location"], "/customers/1?existing=1")
        self.assertEqual(self.counts(), (1, 0, 0))
        self.assertIn("already a customer", self.client.get("/customers/1?existing=1").text)

    def test_adding_a_vehicle_from_the_job_form_returns_with_it_picked(self):
        form = self.client.get("/vehicles/new?customer_id=1&next=job").text
        self.assertIn('name="next" value="job"', form)
        r = self.client.post("/vehicles/new", data={"customer_id": "1", "year": "2019", "make": "Ram", "model": "5500", "next": "job"})
        with self.Session() as db:
            vid = db.query(Vehicle).one().id
        self.assertEqual(r.headers["location"], f"/ro/new?customer_id=1&vehicle_id={vid}")
        self.assertIn(f'<option value="{vid}" selected>2019 Ram 5500', self.client.get(r.headers["location"]).text)
        plain = self.client.post("/vehicles/new", data={"customer_id": "1", "make": "Ford", "model": "Focus"})
        self.assertEqual(plain.headers["location"], "/customers/1")

    def test_form_prefills_from_a_link(self):
        page = self.client.get("/customers/new?phone=6235550188&first=Dana").text
        self.assertIn('value="6235550188"', page)
        self.assertIn('value="Dana"', page)
        self.assertIn("Save and start job", page)


if __name__ == "__main__":
    unittest.main()
