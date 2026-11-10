"""Customer approves or declines each estimate line. Made-up data, private database."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
SIGNATURE = "data:image/png;base64,iVBORw0KGgo="


class LineApprovalTests(unittest.TestCase):
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
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="crew", password_hash=hash_password(PASSWORD), role="tech"))
            customer = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add(customer)
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2016, make="Ford", model="Focus")
            db.add(vehicle)
            db.flush()
            order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="diagnosing", concern="Internal vendor margin note", public_token="made-up-token")
            db.add(order)
            db.flush()
            for description, kind, price in (("Front brakes", "labor", 349), ("Rear brakes", "labor", 349), ("Brake pads", "part", 100)):
                db.add(LineItem(repair_order_id=order.id, description=description, item_type=kind, quantity=1, unit_price=price))
            db.commit()
            self.job = order.id
        self.owner = TestClient(app, follow_redirects=False)
        self.owner.post("/login", data={"username": "owner", "password": PASSWORD})
        self.customer = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def lines(self):
        with self.Session() as db:
            return {li.description: li.approval for li in db.query(LineItem).all()}

    def ids(self):
        with self.Session() as db:
            return {li.description: li.id for li in db.query(LineItem).all()}

    def test_full_approval_flow(self):
        self.assertIn("asked=3", self.owner.post(f"/ro/{self.job}/request-approval").headers["location"])
        self.assertEqual(set(self.lines().values()), {"pending"})
        status = self.customer.get("/status/made-up-token").text
        self.assertIn("3 items to approve", status)
        self.assertIn("/sign/ro/made-up-token", status)
        self.assertNotIn("Internal vendor margin note", self.customer.get("/sign/ro/made-up-token").text)
        ids = self.ids()
        missing = self.customer.post("/sign/ro/made-up-token", data={"approved_by": "Pat", "signature_data": SIGNATURE,
                                                                     f"line_{ids['Front brakes']}": "approved"})
        self.assertEqual(missing.status_code, 422)
        self.assertEqual(set(self.lines().values()), {"pending"})
        done = self.customer.post("/sign/ro/made-up-token", data={
            "approved_by": "Pat", "signature_data": SIGNATURE, f"line_{ids['Front brakes']}": "approved",
            f"line_{ids['Rear brakes']}": "declined", f"line_{ids['Brake pads']}": "approved"})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(self.lines(), {"Front brakes": "approved", "Rear brakes": "declined", "Brake pads": "approved"})
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, self.job).status, "approval_received")
            alert = db.query(DispatchItem).one()
            self.assertEqual((alert.category, alert.status), ("approval", "needs_owner"))
            self.assertIn("Approved 2 of 3 items, $449.00", alert.summary)
            self.assertIn("Rear brakes", alert.summary)
        self.owner.post(f"/invoices/create/{self.job}")
        with self.Session() as db:
            self.assertEqual(db.query(Invoice).one().subtotal, 449)

    def test_draft_lines_cannot_be_signed_or_seen(self):
        page = self.customer.get("/sign/ro/made-up-token")
        self.assertIn("No estimate is ready for approval.", page.text)
        self.assertNotIn("Authorize Work", page.text)
        self.assertNotIn("Front brakes", page.text)
        response = self.customer.post("/sign/ro/made-up-token", data={"signature_data": SIGNATURE})
        self.assertEqual(response.status_code, 422)
        with self.Session() as db:
            self.assertIsNone(db.get(RepairOrder, self.job).signed_at)

    def test_approval_pages_are_never_cached_or_indexed(self):
        page = self.customer.get("/sign/ro/made-up-token")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertIn("noindex", page.headers["x-robots-tag"])
        self.assertEqual(self.customer.get("/sign/ro/wrong-token").status_code, 404)

    def test_decisions_cannot_be_changed_by_resubmitting(self):
        self.owner.post(f"/ro/{self.job}/request-approval")
        ids = self.ids()
        form = {"approved_by": "Pat", "signature_data": SIGNATURE, **{f"line_{i}": "declined" for i in ids.values()}}
        self.customer.post("/sign/ro/made-up-token", data=form)
        self.customer.post("/sign/ro/made-up-token", data={**form, **{f"line_{i}": "approved" for i in ids.values()}})
        self.assertEqual(set(self.lines().values()), {"declined"})
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, self.job).status, "approval_received")
            self.assertEqual(db.query(DispatchItem).count(), 1)
        job = self.owner.get(f"/ro/{self.job}").text
        self.assertIn("Review declined work", job)
        self.assertNotIn("Approved. Start the work", job)

    def test_lines_never_sent_still_bill_as_before(self):
        self.owner.post(f"/invoices/create/{self.job}")
        with self.Session() as db:
            self.assertEqual(db.query(Invoice).one().subtotal, 798)

    def test_tech_cannot_send_for_approval(self):
        crew = TestClient(app, follow_redirects=False)
        crew.post("/login", data={"username": "crew", "password": PASSWORD})
        self.assertEqual(crew.post(f"/ro/{self.job}/request-approval").status_code, 403)
        self.assertEqual(set(self.lines().values()), {None})


if __name__ == "__main__":
    unittest.main()
