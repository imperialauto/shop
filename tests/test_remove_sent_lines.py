"""Owner removes estimate lines that were already sent for approval. Made-up data, private database."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import approval_links
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchAudit, DispatchItem
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"


class RemoveSentLineTests(unittest.TestCase):
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
            db.add(User(id=2, username="desk", password_hash=hash_password(PASSWORD), role="office"))
            customer = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add(customer)
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2016, make="Ford", model="Focus")
            db.add(vehicle)
            db.flush()
            order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="waiting_approval",
                                public_token="made-up-token")
            db.add(order)
            db.flush()
            for description, price, approval in (("Old rebuild", 843.14, "pending"), ("Old tow", 50, "pending"),
                                                 ("Clutch", 700, "approved"), ("Spare idea", 20, "declined"),
                                                 ("New draft line", 45, None)):
                db.add(LineItem(repair_order_id=order.id, description=description, item_type="labor",
                                quantity=1, unit_price=price, approval=approval))
            db.add(DispatchItem(quo_conversation_id=approval_links.conversation_key(order.id), status="needs_owner",
                                customer_phone="+16025550100", last_message_id="made-up-msg",
                                category="approval", summary="made-up queued link"))
            db.commit()
            self.job = order.id
        self.owner = self.login("owner")
        self.desk = self.login("desk")

    def login(self, name):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def ids(self):
        with self.Session() as db:
            return {li.description: li.id for li in db.query(LineItem).all()}

    def remove(self, client, name, confirm="remove"):
        return client.post(f"/ro/{self.job}/remove-line/{self.ids()[name]}", data={"confirm": confirm})

    def test_owner_sees_remove_button_on_sent_lines(self):
        page = self.owner.get(f"/ro/{self.job}").text
        for name in ("Old rebuild", "Old tow", "Clutch", "Spare idea"):
            self.assertIn(f'aria-label="Remove {name}"', page)
        self.assertNotIn('aria-label="Remove New draft line"', page)
        self.assertNotIn("/remove-line/", self.desk.get(f"/ro/{self.job}").text)

    def test_owner_removes_waiting_lines_and_queued_link_closes(self):
        response = self.remove(self.owner, "Old rebuild")
        self.assertEqual(response.status_code, 303)
        self.assertIn("line_removed=1", response.headers["location"])
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).one().status, "needs_owner")  # one line still waiting
        self.assertEqual(self.remove(self.owner, "Old tow").status_code, 303)
        self.assertEqual(set(self.ids()), {"Clutch", "Spare idea", "New draft line"})
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).one().status, "closed")
            self.assertEqual(db.get(RepairOrder, self.job).status, "approval_received")
            audit = [a.detail for a in db.query(DispatchAudit).filter(DispatchAudit.action == "line_removed")
                     if a.item_id is None]
            self.assertEqual(audit, ["Job #%d: removed line 'Old rebuild' $843.14 (waiting on customer)" % self.job,
                                     "Job #%d: removed line 'Old tow' $50.00 (waiting on customer)" % self.job])
        self.assertIn("Line removed. Nothing was sent", self.owner.get(f"/ro/{self.job}?line_removed=1").text)

    def test_owner_removes_approved_and_declined_lines(self):
        self.assertEqual(self.remove(self.owner, "Clutch").status_code, 303)
        self.assertEqual(self.remove(self.owner, "Spare idea").status_code, 303)
        self.assertNotIn("Clutch", self.ids())
        self.assertNotIn("Spare idea", self.ids())

    def test_needs_owner_and_confirm(self):
        self.assertEqual(self.remove(self.desk, "Old tow").status_code, 403)
        self.assertEqual(self.remove(self.owner, "Old tow", confirm="").status_code, 422)
        self.assertIn("Old tow", self.ids())

    def test_refuses_unsent_done_and_invoiced_lines(self):
        self.assertEqual(self.remove(self.owner, "New draft line").status_code, 409)
        with self.Session() as db:
            clutch = db.query(LineItem).filter(LineItem.description == "Clutch").one()
            from datetime import datetime, timezone
            clutch.completed_at = datetime.now(timezone.utc)
            db.commit()
        self.assertEqual(self.remove(self.owner, "Clutch").status_code, 409)
        with self.Session() as db:
            db.query(LineItem).filter(LineItem.description == "Clutch").one().completed_at = None
            db.add(Invoice(repair_order_id=self.job, status="draft"))
            db.commit()
        self.assertEqual(self.remove(self.owner, "Clutch").status_code, 409)
        self.assertNotIn('aria-label="Remove Clutch"', self.owner.get(f"/ro/{self.job}").text)
        # Lines that were never billed can still go while an invoice exists.
        self.assertEqual(self.remove(self.owner, "Old tow").status_code, 303)
        self.assertIn("Clutch", self.ids())

    def test_wrong_job_is_not_found(self):
        response = self.owner.post(f"/ro/{self.job + 99}/remove-line/{self.ids()['Old tow']}", data={"confirm": "remove"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
