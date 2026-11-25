"""Fixes from the Nov 24 end-to-end walk: declined estimates, tax on the approval page, lines added
after a draft invoice, and negative lines. Made-up data only."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import shop_board
from app.database import Base, get_db
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle
from contextlib import ExitStack

from app.routes import invoices, repair_orders

SIGNATURE = "data:image/png;base64,iVBORw0KGgo="


class WorkflowFixTests(unittest.TestCase):
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
            owner = User(username="owner-fixture", password_hash="x", role="owner")
            c = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add_all([owner, c]); db.flush()
            v = Vehicle(customer_id=c.id, year=2012, make="Chevrolet", model="Silverado")
            db.add(v); db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="waiting_approval", public_token="fix-token-12345678")
            db.add(ro); db.flush()
            db.add_all([LineItem(repair_order_id=ro.id, description="Brake pads", item_type="part", quantity=1,
                                 unit_price=200, approval="pending"),
                        LineItem(repair_order_id=ro.id, description="Brake labor", item_type="labor", quantity=1,
                                 unit_price=300, approval="pending")])
            db.commit()
            self.ro_id, self.owner_id = ro.id, owner.id
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def as_owner(self):
        stack = ExitStack()
        for module in (repair_orders, invoices):
            stack.enter_context(patch.object(module, "require_user",
                                             side_effect=lambda request, db: db.get(User, self.owner_id)))
        return stack

    def lines(self):
        with self.Session() as db:
            return {li.description: li.id for li in db.query(LineItem).filter_by(repair_order_id=self.ro_id)}

    def test_approval_page_total_includes_tax_on_parts(self):
        page = self.client.get("/sign/ro/fix-token-12345678").text
        self.assertIn("$518.40", page)  # 500 + 9.2% of the $200 part, same as the deposit and invoice
        self.assertIn("Includes 9.2% sales tax on parts", page)
        self.assertIn('data-taxable="1"', page)

    def test_declining_everything_does_not_read_as_ready_to_start(self):
        form = {f"line_{i}": "declined" for i in self.lines().values()}
        form["signature_data"] = SIGNATURE
        self.assertEqual(self.client.post("/sign/ro/fix-token-12345678", data=form).status_code, 200)
        with self.Session() as db:
            ro = db.get(RepairOrder, self.ro_id)
            card = shop_board._job(ro)
        self.assertEqual(card["stage"], "Customer declined, call them")

    def test_approving_still_reads_ready_to_start(self):
        form = {f"line_{i}": "approved" for i in self.lines().values()}
        form["signature_data"] = SIGNATURE
        self.client.post("/sign/ro/fix-token-12345678", data=form)
        with self.Session() as db:
            self.assertEqual(shop_board._job(db.get(RepairOrder, self.ro_id))["stage"], "Approved, ready to start")

    def test_negative_or_zero_lines_are_refused(self):
        with self.as_owner():
            for qty, price, kind in ((-1, 50, "labor"), (0, 50, "labor"), (1, -50, "part")):
                with self.subTest(qty=qty, price=price):
                    r = self.client.post(f"/ro/{self.ro_id}/add-line-item",
                                         data={"description": "Bad", "item_type": kind, "quantity": qty, "unit_price": price})
                    self.assertIn("line_error=amount", r.headers["location"])
            ok = self.client.post(f"/ro/{self.ro_id}/add-line-item",
                                  data={"description": "Loyalty discount", "item_type": "misc", "quantity": 1, "unit_price": -20})
            self.assertNotIn("line_error", ok.headers["location"])
        self.assertNotIn("Bad", self.lines())
        self.assertIn("Loyalty discount", self.lines())

    def test_line_added_after_a_draft_invoice_reaches_the_invoice(self):
        with self.Session() as db:
            for li in db.query(LineItem).filter_by(repair_order_id=self.ro_id):
                li.approval = "approved"
            db.commit()
        with self.as_owner():
            self.client.post(f"/invoices/create/{self.ro_id}")
            with self.Session() as db:
                invoice = db.query(Invoice).filter_by(repair_order_id=self.ro_id).one()
                self.assertAlmostEqual(invoice.total, 518.40)
            r = self.client.post(f"/ro/{self.ro_id}/add-line-item",
                                 data={"description": "Brake fluid flush", "item_type": "labor", "quantity": 1, "unit_price": 180})
            self.assertIn("line_added=invoice", r.headers["location"])
        with self.Session() as db:
            invoice = db.query(Invoice).filter_by(repair_order_id=self.ro_id).one()
            self.assertAlmostEqual(invoice.total, 698.40)
            self.assertAlmostEqual(invoice.subtotal, 680.0)

    def test_line_cannot_be_added_once_the_invoice_was_sent(self):
        with self.Session() as db:
            db.add(Invoice(repair_order_id=self.ro_id, subtotal=500, tax_rate=0.092, tax_amount=18.4, total=518.4,
                           status="sent"))
            db.commit()
        with self.as_owner():
            r = self.client.post(f"/ro/{self.ro_id}/add-line-item",
                                 data={"description": "Late add", "item_type": "labor", "quantity": 1, "unit_price": 180})
        self.assertIn("line_error=invoiced", r.headers["location"])
        self.assertNotIn("Late add", self.lines())


if __name__ == "__main__":
    unittest.main()
