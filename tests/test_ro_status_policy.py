"""No staff status dropdown can substitute for a customer decision or finished work.

Made-up shop records only. No real messages, customers, or invoices.
"""
import unittest
from types import SimpleNamespace

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.ro_status_policy import manual_status_options, status_error

PASSWORD = "fixture-password-1"


class StatusPolicyUnitTests(unittest.TestCase):
    def order(self, status="intake", decisions=()):
        return SimpleNamespace(status=status, line_items=[
            SimpleNamespace(approval=decision) for decision in decisions
        ])

    def test_draft_and_pending_quotes_do_not_count_as_approval(self):
        for decision in (None, "pending"):
            ro = self.order("waiting_approval", (decision,))
            for target in ("in_progress", "waiting_parts", "complete"):
                self.assertEqual(status_error(ro, target, "owner")[0], 409)
        # Approved work can proceed without executing separately pending work.
        self.assertIsNone(status_error(
            self.order("waiting_approval", ("approved", "pending")), "in_progress", "owner"))

    def test_repair_requires_scope_but_diagnosis_can_begin_without_quote(self):
        intake = self.order("intake")
        self.assertEqual(status_error(intake, "in_progress", "owner")[0], 409)
        self.assertEqual(status_error(intake, "waiting_parts", "tech")[0], 409)
        self.assertIsNone(status_error(intake, "diagnosing", "tech"))
        self.assertIsNone(status_error(self.order("diagnosing"), "complete", "owner"))

    def test_approval_status_requires_recorded_decisions(self):
        self.assertEqual(status_error(self.order("diagnosing"), "approval_received", "owner")[0], 409)
        self.assertEqual(status_error(
            self.order("waiting_approval", ("approved", "pending")), "approval_received", "owner")[0], 409)
        self.assertIsNone(status_error(
            self.order("waiting_approval", ("approved", "declined")), "approval_received", "office"))
        self.assertIsNone(status_error(
            self.order("waiting_approval", ("declined",)), "approval_received", "owner"))
        self.assertEqual(status_error(self.order("diagnosing", (None,)), "waiting_approval", "owner")[0], 409)

    def test_delivery_requires_completion_and_office(self):
        self.assertEqual(status_error(
            self.order("in_progress", ("approved",)), "delivered", "owner")[0], 409)
        self.assertEqual(status_error(
            self.order("complete", ("approved",)), "delivered", "tech")[0], 403)
        self.assertIsNone(status_error(
            self.order("complete", ("approved",)), "delivered", "owner"))
        self.assertEqual(status_error(
            self.order("approval_received", ("approved",)), "complete", "owner")[0], 409)
        self.assertIsNone(status_error(
            self.order("in_progress", ("approved",)), "complete", "tech"))

    def test_manual_options_never_offer_a_forbidden_transition(self):
        ro = self.order("waiting_approval", ("pending",))
        options = [("waiting_approval", "Waiting Approval"), ("in_progress", "In Progress"),
                   ("approval_received", "Approval Received"), ("delivered", "Delivered")]
        self.assertEqual(manual_status_options(ro, "tech", options),
                         [("waiting_approval", "Waiting Approval")])
        # The current status remains selectable as a no-op.
        options = manual_status_options(ro, "office", options)
        self.assertEqual(options, [("waiting_approval", "Waiting Approval")])


class StatusRouteTests(unittest.TestCase):
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
                User(id=2, username="technician", password_hash=hash_password(PASSWORD), role="tech"),
                Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100"),
                Vehicle(id=1, customer_id=1, year=2019, make="Ford", model="Focus"),
                RepairOrder(id=5, customer_id=1, vehicle_id=1, status="waiting_approval",
                            public_token="fixture-approval-token"),
                LineItem(id=1, repair_order_id=5, description="Fixture repair",
                         approval="pending", quantity=1, unit_price=150),
            ])
            db.commit()
        self.owner = TestClient(app, follow_redirects=False)
        self.owner.post("/login", data={"username": "owner", "password": PASSWORD})
        self.tech = TestClient(app, follow_redirects=False)
        self.tech.post("/login", data={"username": "technician", "password": PASSWORD})

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.old_read_only
        self.engine.dispose()

    def status(self):
        with self.Session() as db:
            return db.get(RepairOrder, 5).status

    def mutate(self, *, approval=None, status=None):
        with self.Session() as db:
            order = db.get(RepairOrder, 5)
            if approval is not None:
                order.line_items[0].approval = approval
            if status is not None:
                order.status = status
            db.commit()

    def test_manual_skips_are_rejected_without_database_change(self):
        for next_status in ("in_progress", "waiting_parts", "complete", "delivered", "approval_received"):
            with self.subTest(next_status=next_status):
                response = self.owner.post("/ro/5/update-status", data={"status": next_status})
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.status(), "waiting_approval")

    def test_valid_progression_and_tech_customer_approval_guard(self):
        self.assertEqual(self.tech.post("/ro/5/update-status",
                                       data={"status": "approval_received"}).status_code, 403)
        self.assertEqual(self.tech.post("/ro/5/update-status",
                                       data={"status": "waiting_approval"}).status_code, 302)
        self.mutate(approval="approved")
        self.assertEqual(self.owner.post("/ro/5/update-status",
                                         data={"status": "in_progress"}).status_code, 302)
        self.assertEqual(self.tech.post("/ro/5/update-status",
                                        data={"status": "complete"}).status_code, 302)
        self.assertEqual(self.tech.post("/ro/5/update-status",
                                        data={"status": "delivered"}).status_code, 403)
        self.assertEqual(self.owner.post("/ro/5/update-status",
                                         data={"status": "delivered"}).status_code, 302)
        self.assertEqual(self.status(), "delivered")

    def test_dropdown_excludes_unsafe_choices(self):
        initial = self.owner.get("/ro/5").text
        self.assertIn('value="waiting_approval"', initial)
        self.assertNotIn('value="delivered"', initial)
        self.assertNotIn('value="in_progress"', initial)
        self.mutate(approval="approved", status="complete")
        page = self.owner.get("/ro/5").text
        self.assertIn('value="delivered"', page)


if __name__ == "__main__":
    unittest.main()
