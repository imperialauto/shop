"""Fixes from the Nov 27 code audit. Made-up data, private database."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"


class AuditFixTests(unittest.TestCase):
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
            vehicle = Vehicle(customer_id=customer.id, year=2016, make="Ford", model="O'Brien")
            db.add(vehicle)
            db.flush()
            order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="in_progress",
                                public_token="made-up-token")
            db.add(order)
            db.flush()
            db.add(LineItem(repair_order_id=order.id, description="Labor", item_type="labor",
                            quantity=1, unit_price=1000, approval="approved"))
            db.add(Invoice(repair_order_id=order.id, subtotal=1000, tax_rate=0.092, tax_amount=0, total=1000,
                           status="draft"))
            db.commit()
            self.job = order.id

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def login(self, name):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def invoice(self):
        with self.Session() as db:
            inv = db.query(Invoice).first()
            return inv.status, inv.total

    def test_declined_line_comes_off_the_draft_invoice(self):
        with self.Session() as db:
            db.add(LineItem(repair_order_id=self.job, description="Part", item_type="part",
                            quantity=1, unit_price=250, approval=None))
            db.commit()
        self.assertEqual(self.invoice(), ("draft", 1273.0))  # 250 part + 9.2% tax added
        with self.Session() as db:
            li = db.query(LineItem).filter(LineItem.description == "Part").one()
            li.approval = "declined"
            db.commit()
        self.assertEqual(self.invoice(), ("draft", 1000.0))

    def test_sent_invoice_is_never_retotalled(self):
        with self.Session() as db:
            db.query(Invoice).one().status = "sent"
            db.commit()
            db.add(LineItem(repair_order_id=self.job, description="Extra", item_type="labor",
                            quantity=1, unit_price=99, approval="approved"))
            db.commit()
        self.assertEqual(self.invoice(), ("sent", 1000.0))

    def test_mark_sent_does_not_revive_a_paid_invoice(self):
        with self.Session() as db:
            inv = db.query(Invoice).one()
            inv.status = "paid"
            db.commit()
            inv_id = inv.id
        self.login("owner").post(f"/invoices/{inv_id}/mark-sent")
        self.assertEqual(self.invoice()[0], "paid")

    def test_nan_price_is_refused(self):
        r = self.login("owner").post(f"/ro/{self.job}/add-line-item",
                                     data={"description": "Bad", "item_type": "labor", "quantity": "1",
                                           "unit_price": "nan"})
        self.assertIn("line_error=amount", r.headers.get("location", ""))
        with self.Session() as db:
            self.assertEqual(db.query(LineItem).filter(LineItem.description == "Bad").count(), 0)

    def test_estimate_pdf_needs_sign_in(self):
        r = TestClient(app, follow_redirects=False).post("/estimates/pdf", data={"estimate_json": "{}"})
        self.assertIn(r.status_code, (302, 303, 401))

    def test_vehicle_label_is_not_put_inside_script(self):
        page = self.login("owner").get(f"/ro/{self.job}").text
        self.assertNotIn("var veh = '", page)
        self.assertIn('data-veh="', page)

    def test_staff_session_ends_when_password_changes(self):
        desk = self.login("desk")
        self.assertEqual(desk.get("/ro/").status_code, 200)
        with self.Session() as db:
            db.get(User, 2).password_hash = hash_password("fixture-password-2")
            db.commit()
        self.assertNotEqual(desk.get("/ro/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
