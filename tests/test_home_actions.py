"""Home's goal bar, one-tap next steps and red aging."""
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.next_action import days_since
from app.routes.finance import home_goal


class HomeActionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        old = datetime.utcnow() - timedelta(days=3)
        db = self.Session()
        db.add(User(id=1, username="owner", password_hash=hash_password("pw-owner-123"), role="owner", full_name="Owner"))
        db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100"))
        db.add(Vehicle(id=1, customer_id=1, year=2015, make="Ford", model="Explorer"))
        db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="intake", public_token="t1", created_at=old, updated_at=old))
        db.add(LineItem(id=1, repair_order_id=1, description="Oil cooler", quantity=1, unit_price=150,
                        created_at=old))
        db.commit()
        db.close()

        def override():
            s = self.Session()
            try:
                yield s
            finally:
                s.close()
        app.dependency_overrides[get_db] = override
        self.read_only, app.state.read_only = app.state.read_only, False
        self.client = TestClient(app)
        self.client.post("/login", data={"username": "owner", "password": "pw-owner-123"})

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only

    def test_goal_bar_uses_daily_goal_when_bills_are_covered(self):
        db = self.Session()
        goal = home_goal(db)
        db.close()
        self.assertFalse(goal["bills"])
        self.assertEqual(goal["target"], goal["goal"])
        self.assertIn("Bills covered", self.client.get("/dashboard").text)

    def test_job_row_has_one_tap_step_and_turns_red_after_two_days(self):
        html = self.client.get("/dashboard").text
        self.assertIn('action="/ro/1/request-approval"', html)
        self.assertIn('data-confirm="Ask for approval for job #1?', html)
        self.assertIn('class="h-row h-job late"', html)
        self.assertIn("3 days", html)
        # One next-step button per job row; texting the customer lives on their file, not on every row.
        self.assertNotIn('href="/customers/1#reply-text"', html)
        self.assertIn('action="/board/lot/1"', html)  # the car-here toggle

    def test_status_step_comes_back_home(self):
        r = self.client.post("/ro/1/update-status?back=home", data={"status": "intake"},
                             headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/dashboard")

    def test_days_since(self):
        self.assertEqual(days_since(None), 0)
        self.assertEqual(days_since(datetime.now(timezone.utc) - timedelta(hours=50)), 2)


if __name__ == "__main__":
    unittest.main()
