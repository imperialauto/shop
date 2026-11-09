"""Estimate tool pricing and job creation, with made-up data and no AI call."""
import json
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app import business_settings
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, User, Vehicle
from app.routes.estimate_gen import apply_shop_rates

PASSWORD = "fixture-password-1"


class RateTests(unittest.TestCase):
    def setUp(self):
        business_settings.clear_cache()

    def test_shop_rates_replace_whatever_the_ai_said(self):
        data = apply_shop_rates({"labor_hours": 2, "rate_type": "diesel", "labor_rate": 99, "parts_low": 100, "parts_high": 50})
        self.assertEqual((data["labor_rate"], data["labor_total"]), (175.0, 350.0))
        self.assertEqual((data["parts_high"], data["total_low"], data["total_high"]), (100, 450, 450))

    def test_unknown_rate_type_uses_shop_rate(self):
        self.assertEqual(apply_shop_rates({"labor_hours": 1, "rate_type": "luxury"})["labor_rate"], 150.0)


class CreateJobTests(unittest.TestCase):
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
        self.patch = patch.object(business_settings, "SessionLocal", self.Session)
        self.patch.start()
        business_settings.clear_cache()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.commit()
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"username": "owner", "password": PASSWORD})

    def tearDown(self):
        self.patch.stop()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def create(self, **changes):
        data = apply_shop_rates({"name": "Pat Sample", "year": 2016, "make": "Ford", "model": "Focus", "mileage": 90000,
                                 "labor_hours": 3, "rate_type": "shop", "parts_low": 200, "parts_high": 300, **changes})
        return self.client.post("/estimates/create-ro", data={"estimate_json": json.dumps(data), "phone_number": "6025550100"})

    def test_returning_customer_reuses_the_vehicle(self):
        self.assertEqual(self.create().status_code, 200)
        self.assertEqual(self.create(mileage=91000).status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.query(Customer).count(), 1)
            self.assertEqual(db.query(Vehicle).count(), 1)
            self.assertEqual(db.query(Vehicle).one().mileage, 91000)
            labor = db.query(LineItem).filter_by(item_type="labor").first()
            self.assertEqual(labor.unit_price, 150.0)
            self.assertIn("unverified", db.query(LineItem).filter_by(item_type="part").first().description)

    def test_a_different_vehicle_is_added(self):
        self.create()
        self.create(model="Fusion")
        with self.Session() as db:
            self.assertEqual(db.query(Vehicle).count(), 2)


if __name__ == "__main__":
    unittest.main()
