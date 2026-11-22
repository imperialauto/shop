"""Owner-only job delete for test or mistaken jobs.

Made-up shop records only. No real messages, customers, or invoices.
"""
import unittest
from datetime import datetime, timedelta, timezone

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.appointment_model import Appointment
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchAudit, DispatchItem
from app.main import app
from app.models import (Communication, Customer, Invoice, LineItem, RepairOrder, RoAttachment, User,
                        Vehicle)

PASSWORD = "fixture-password-1"
T0 = datetime(2026, 7, 5, 16, 0, tzinfo=timezone.utc)


class DeleteJobTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.old_read_only = app.state.read_only
        app.state.read_only = False

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        with self.Session() as db:
            db.add_all([
                User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"),
                User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"),
                User(id=3, username="technician", password_hash=hash_password(PASSWORD), role="tech"),
                Customer(id=1, first_name="Fixture", last_name="Tester", phone="+16025550100"),
                Vehicle(id=1, customer_id=1, year=1943, make="Ford", model="Model T"),
                Vehicle(id=2, customer_id=1, year=2015, make="Chevrolet", model="Tahoe"),
                RepairOrder(id=5, customer_id=1, vehicle_id=1, status="waiting_approval",
                            public_token="fixture-delete-token"),
                RepairOrder(id=6, customer_id=1, vehicle_id=2, status="waiting_approval"),
                LineItem(id=1, repair_order_id=5, description="Fixture brakes", approval="pending",
                         quantity=1, unit_price=150),
                LineItem(id=2, repair_order_id=6, description="Fixture A/C", approval="pending",
                         quantity=1, unit_price=90),
                RoAttachment(id=1, repair_order_id=5, filename="a.jpg", content_type="image/jpeg", size=1,
                             data=b"x", uploaded_by="owner"),
                Appointment(id=1, repair_order_id=5, resource_id="bay-1", starts_at=T0,
                            ends_at=T0 + timedelta(hours=1)),
                Communication(id=1, repair_order_id=5, customer_id=1, body="Fixture text"),
                DispatchItem(id=1, quo_conversation_id="LINK5", customer_phone="+16025550100",
                             category="approval_link", status="needs_owner", last_message_id="none"),
            ])
            db.commit()
        self.clients = {}
        for name in ("owner", "office", "technician"):
            client = TestClient(app, follow_redirects=False)
            client.post("/login", data={"username": name, "password": PASSWORD})
            self.clients[name] = client

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.old_read_only
        self.engine.dispose()

    def jobs(self):
        with self.Session() as db:
            return sorted(r.id for r in db.query(RepairOrder).all())

    def test_owner_deletes_only_that_job_and_its_parts(self):
        response = self.clients["owner"].post("/ro/5/delete", data={"confirm": "5"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/ro/?deleted=5")
        self.assertEqual(self.jobs(), [6])
        with self.Session() as db:
            self.assertEqual([li.id for li in db.query(LineItem).all()], [2])
            self.assertEqual(db.query(RoAttachment).count(), 0)
            self.assertEqual(db.query(Appointment).count(), 0)
            # The customer, vehicle and their text history stay; the text just loses its job link.
            self.assertIsNotNone(db.get(Customer, 1))
            self.assertIsNotNone(db.get(Vehicle, 1))
            text = db.get(Communication, 1)
            self.assertIsNotNone(text)
            self.assertIsNone(text.repair_order_id)
            # A queued approval-link text can never go out for a deleted job.
            self.assertEqual(db.get(DispatchItem, 1).status, "closed")
            self.assertTrue(db.query(DispatchAudit).filter(DispatchAudit.action == "job_deleted").count() >= 1)
        page = self.clients["owner"].get("/ro/?deleted=5")
        self.assertIn("Job #5 was deleted.", page.text)

    def test_wrong_confirmation_changes_nothing(self):
        for value in ("", "6", "yes"):
            with self.subTest(value=value):
                response = self.clients["owner"].post("/ro/5/delete", data={"confirm": value})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.jobs(), [5, 6])

    def test_only_the_owner_can_delete(self):
        for name in ("office", "technician"):
            with self.subTest(role=name):
                response = self.clients[name].post("/ro/5/delete", data={"confirm": "5"})
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.jobs(), [5, 6])

    def test_jobs_with_money_or_finished_work_are_refused(self):
        with self.Session() as db:
            db.add(Invoice(id=1, repair_order_id=5, total=150.0, status="draft"))
            db.get(RepairOrder, 6).status = "complete"
            db.commit()
        self.assertEqual(self.clients["owner"].post("/ro/5/delete", data={"confirm": "5"}).status_code, 409)
        self.assertEqual(self.clients["owner"].post("/ro/6/delete", data={"confirm": "6"}).status_code, 409)
        self.assertEqual(self.jobs(), [5, 6])
        with self.Session() as db:
            self.assertEqual(db.query(LineItem).count(), 2)

    def test_delete_panel_shows_for_owner_only(self):
        self.assertIn("Delete job #5", self.clients["owner"].get("/ro/5").text)
        self.assertNotIn("Delete job #5", self.clients["office"].get("/ro/5").text)

    def test_missing_job_is_not_found(self):
        self.assertEqual(self.clients["owner"].post("/ro/99/delete", data={"confirm": "99"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
