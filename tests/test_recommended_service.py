"""Next recommended service: declined work stays on the customer's file. Made-up data, private database."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import customer_file, recommended_service as rec
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
T0 = datetime(2026, 10, 1, 17, 0)


def down(path, phone):
    return None, "unavailable"


class RecommendedServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        self.db.add_all([User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"),
                         Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100"),
                         Customer(id=2, first_name="Lee", last_name="Other", phone="(480) 555-0199"),
                         Vehicle(id=1, customer_id=1, year=2013, make="Chevrolet", model="Impala"),
                         Vehicle(id=2, customer_id=1, year=2010, make="Honda", model="Fit"),
                         Vehicle(id=3, customer_id=2, year=2012, make="Ford", model="F-150"),
                         RepairOrder(id=13, customer_id=1, vehicle_id=1, status="delivered", created_at=T0),
                         RepairOrder(id=20, customer_id=1, vehicle_id=1, status="in_progress", created_at=T0 + timedelta(days=30)),
                         RepairOrder(id=21, customer_id=1, vehicle_id=2, status="in_progress", created_at=T0 + timedelta(days=30)),
                         RepairOrder(id=30, customer_id=2, vehicle_id=3, status="delivered", created_at=T0)])
        self.db.flush()
        decided = T0 + timedelta(days=1)
        self.db.add_all([
            LineItem(id=1, repair_order_id=13, description="Power steering pressure line labor", unit_price=300,
                     approval="declined", decided_at=decided, created_at=T0),
            LineItem(id=2, repair_order_id=13, description="Power steering pressure line (part)", item_type="part",
                     unit_price=250, approval="declined", decided_at=decided, created_at=T0),
            LineItem(id=3, repair_order_id=13, description="Oil pan gasket labor", unit_price=180,
                     approval="approved", decided_at=decided, created_at=T0),
            LineItem(id=4, repair_order_id=13, description="Coolant flush", unit_price=129,
                     approval="pending", created_at=T0),
            LineItem(id=5, repair_order_id=13, description="Courtesy discount", item_type="discount",
                     unit_price=-20, approval="declined", decided_at=decided, created_at=T0),
            LineItem(id=6, repair_order_id=13, description="Shop supplies", unit_price=0,
                     approval="declined", decided_at=decided, created_at=T0),
            LineItem(id=7, repair_order_id=30, description="Rear brakes", unit_price=400,
                     approval="declined", decided_at=decided, created_at=T0),
        ])
        self.db.commit()

    def ids(self, lines):
        return [li.id for li in lines]

    def test_declined_and_unanswered_lines_become_recommendations(self):
        found = self.ids(rec.for_customer(self.db, 1))
        self.assertEqual(sorted(found), [1, 2, 4])  # discount, $0 and approved lines left out
        self.assertEqual(self.ids(rec.for_customer(self.db, 2)), [7])  # another customer's work stays theirs

    def test_offered_on_the_same_cars_next_job_only(self):
        self.assertEqual(sorted(self.ids(rec.for_job(self.db, self.db.get(RepairOrder, 20)))), [1, 2, 4])
        self.assertEqual(rec.for_job(self.db, self.db.get(RepairOrder, 21)), [])  # the other car

    def test_add_to_job_copies_unsent_and_drops_off(self):
        job = self.db.get(RepairOrder, 20)
        line = self.db.get(LineItem, 2)
        copy = rec.add_to_job(self.db, line, job, "owner")
        self.db.commit()
        self.assertEqual((copy.repair_order_id, copy.description, copy.unit_price, copy.approval),
                         (20, line.description, 250, None))
        self.assertEqual(sorted(self.ids(rec.for_customer(self.db, 1))), [1, 4])  # the labor line stays
        self.assertEqual(self.db.get(LineItem, 2).approval, "declined")  # the old job is untouched

    def test_declined_again_comes_back(self):
        rec.add_to_job(self.db, self.db.get(LineItem, 4), self.db.get(RepairOrder, 20), "owner")
        self.db.commit()
        self.assertNotIn(4, self.ids(rec.for_customer(self.db, 1)))
        new = self.db.query(LineItem).filter_by(repair_order_id=20).one()
        new.approval, new.decided_at = "declined", datetime.utcnow()
        self.db.commit()
        self.assertIn(new.id, self.ids(rec.for_customer(self.db, 1)))

    def test_lines_not_agreed_at_close_out_count(self):
        from app import job_closeout
        job = self.db.get(RepairOrder, 20)
        self.db.add(LineItem(id=40, repair_order_id=20, description="Tie rod end", unit_price=95, created_at=T0))
        self.db.add(LineItem(id=41, repair_order_id=20, description="Alignment", unit_price=120, created_at=T0))
        self.db.commit()
        job_closeout.close_out(self.db, job, "owner", [41], picked_up=True)
        self.db.commit()
        self.assertEqual(self.db.get(LineItem, 40).approval_source, "shop")
        self.assertIn(40, self.ids(rec.for_customer(self.db, 1)))

    def test_remove_hides_it_without_changing_lines(self):
        rec.dismiss(self.db, self.db.get(LineItem, 4), "owner")
        self.db.commit()
        self.assertEqual(sorted(self.ids(rec.for_customer(self.db, 1))), [1, 2])
        self.assertEqual(self.db.get(LineItem, 4).approval, "pending")

    def test_pages_show_add_and_remove(self):
        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(setattr, app.state, "read_only", app.state.read_only)
        app.state.read_only = False
        with patch.object(customer_file, "_bridge", down):
            client = TestClient(app)
            client.post("/login", data={"username": "owner", "password": PASSWORD})
            page = client.get("/customers/1").text
            self.assertIn("Next recommended service", page)
            self.assertIn("Power steering pressure line (part)", page)
            self.assertIn("not answered", page)
            job = client.get("/ro/20").text
            self.assertIn("Next recommended service for this car", job)
            self.assertNotIn("Next recommended service for this car", client.get("/ro/21").text)
            client.post("/ro/20/recommendations/1/add")
            client.post("/ro/20/recommendations/2/add")
            client.post("/customers/1/recommendations/4/remove")
        self.db.expire_all()
        self.assertEqual(rec.for_customer(self.db, 1), [])
        self.assertEqual(self.db.query(LineItem).filter_by(repair_order_id=20).count(), 2)


if __name__ == "__main__":
    unittest.main()
