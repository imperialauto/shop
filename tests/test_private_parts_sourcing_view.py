"""RO part sourcing panel: private vendor cost is never shown to technicians."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401 - local test database only
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Customer, PartsOrder, RepairOrder, User, Vehicle
from app.routes import repair_orders


class PrivateCostDisplayTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([
                User(id=1, username="owner", password_hash="fixture", role="owner"),
                User(id=2, username="tech", password_hash="fixture", role="tech"),
                Customer(id=1, first_name="Sample", last_name="Customer", phone="6025550100"),
            ])
            db.flush()
            db.add(Vehicle(id=1, customer_id=1, year=2013, make="Ford", model="Fusion"))
            db.flush()
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="diagnosing"))
            db.add(PartsOrder(repair_order_id=1, description="fuel tank — anticipated pickup Thursday (unconfirmed)",
                              cost=81.83, quantity=1, status="sourcing"))
            db.commit()

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def show_as(self, user_id):
        with patch.object(repair_orders, "require_user", side_effect=lambda request, db: db.get(User, user_id)):
            return self.client.get("/ro/1")

    def test_owner_sees_private_acquisition_cost_not_customer_quote(self):
        response = self.show_as(1)
        self.assertEqual(response.status_code, 200, response.text[:350])
        self.assertIn('id="parts-sourcing"', response.text)
        self.assertIn("Our cost (private)", response.text)
        self.assertIn("81.83", response.text)
        self.assertIn("Sourcing records are not customer quotes", response.text)

    def test_technician_sees_part_eta_without_supplier_cost(self):
        response = self.show_as(2)
        self.assertEqual(response.status_code, 200, response.text[:350])
        self.assertIn("fuel tank", response.text)
        self.assertIn("Thursday", response.text)
        self.assertNotIn("Our cost (private)", response.text)
        self.assertNotIn("81.83", response.text)


if __name__ == "__main__":
    unittest.main()
