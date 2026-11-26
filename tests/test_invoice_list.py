"""Invoices page: unpaid first with what's owed, paid second, void hidden, approved work not invoiced yet."""
import re
import unittest
from datetime import datetime
from unittest.mock import patch

import fixture_environment  # noqa: F401 -- disposable sqlite before app imports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, Invoice, JobDeposit, LineItem, Payment, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"


class InvoiceListTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.store_patch = patch.object(store, "SessionLocal", self.Session)
        self.store_patch.start()
        store.clear_cache()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.commit()

    def tearDown(self):
        self.store_patch.stop()
        store.clear_cache()
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": "owner", "password": PASSWORD})
        return client

    def job(self, db, first, last, car, status="in_progress"):
        customer = Customer(first_name=first, last_name=last)
        db.add(customer)
        db.flush()
        year, make, model = car
        vehicle = Vehicle(customer_id=customer.id, year=year, make=make, model=model)
        db.add(vehicle)
        db.flush()
        ro = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status=status)
        db.add(ro)
        db.flush()
        return ro

    def invoice(self, db, ro, total, status, payments=(), created=None):
        inv = Invoice(repair_order_id=ro.id, subtotal=total, tax_rate=0, tax_amount=0, total=total, status=status,
                      created_at=created or datetime(2026, 11, 20, 18))
        db.add(inv)
        db.flush()
        for amount in payments:
            db.add(Payment(invoice_id=inv.id, amount=amount, method="cash"))
        return inv

    def seed(self):
        with self.Session() as db:
            unpaid_ro = self.job(db, "Ula", "Unpaid", (2016, "Ford", "Focus"))
            part_ro = self.job(db, "Pat", "Partial", (2005, "Cadillac", "Escalade"))
            paid_ro = self.job(db, "Penny", "Paid", (2014, "Chevrolet", "Impala"), status="delivered")
            void_ro = self.job(db, "Vic", "Void", (2010, "Honda", "Civic"))
            self.ids = {
                "unpaid": self.invoice(db, unpaid_ro, 400.00, "draft").id,
                "part": self.invoice(db, part_ro, 1250.50, "sent", payments=(500.00, 250.25)).id,
                "paid": self.invoice(db, paid_ro, 300.00, "paid", payments=(300.00,)).id,
                "void": self.invoice(db, void_ro, 999.00, "void").id,
            }
            # Approved work on an open job with no invoice: $200 labor + $100 part (+9.2% tax) less a $50 deposit.
            pending_ro = self.job(db, "Nia", "Noinvoice", (2019, "Ram", "5500"))
            db.add_all([
                LineItem(repair_order_id=pending_ro.id, description="Labor", item_type="labor", quantity=1, unit_price=200, approval="approved"),
                LineItem(repair_order_id=pending_ro.id, description="Part", item_type="part", quantity=1, unit_price=100, approval="approved"),
                LineItem(repair_order_id=pending_ro.id, description="Maybe later", item_type="labor", quantity=1, unit_price=80, approval="pending"),
                JobDeposit(repair_order_id=pending_ro.id, amount=50, method="cash"),
            ])
            self.pending_job = pending_ro.id
            # Approved work on a job that already has an invoice is not listed again.
            db.add(LineItem(repair_order_id=part_ro.id, description="Done", item_type="labor", quantity=1, unit_price=1250.50, approval="approved"))
            db.commit()

    def sections(self, html):
        unpaid = html.split('id="unpaid"')[1].split('id="paid"')[0]
        paid = html.split('id="paid"')[1]
        return unpaid, paid

    def test_signed_out_goes_to_login(self):
        self.assertEqual(TestClient(app, follow_redirects=False).get("/invoices/").headers["location"], "/login")

    def test_unpaid_first_with_balances_and_owed_total(self):
        self.seed()
        response = self.sign_in().get("/invoices/")
        self.assertEqual(response.status_code, 200)
        html = response.text
        unpaid, paid = self.sections(html)

        # Draft (nothing paid) and partly paid are both under Unpaid.
        self.assertIn(f"/invoices/{self.ids['unpaid']}'", unpaid)
        self.assertIn("Ula Unpaid", unpaid)
        self.assertIn("2016 Ford Focus", unpaid)
        self.assertIn(f"/invoices/{self.ids['part']}'", unpaid)
        self.assertIn("Partly paid", unpaid)
        self.assertIn("$750.25", unpaid)   # paid so far
        self.assertIn("$500.25", unpaid)   # 1250.50 - 750.25 still due
        self.assertNotIn(f"/invoices/{self.ids['paid']}'", unpaid)

        # Paid sits under Paid only.
        self.assertIn(f"/invoices/{self.ids['paid']}'", paid)
        self.assertIn("Penny Paid", paid)
        self.assertNotIn("Ula Unpaid", paid)

        # Owed at the top: 400.00 + 500.25.
        self.assertIn('data-owed="900.25"', html)
        self.assertLess(html.index('id="owed-summary"'), html.index('id="unpaid"'))
        self.assertLess(html.index('id="unpaid"'), html.index('id="paid"'))

    def test_void_hidden_until_asked(self):
        self.seed()
        client = self.sign_in()
        html = client.get("/invoices/").text
        self.assertNotIn("Vic Void", html)
        self.assertIn("Show 1 void invoice", html)
        shown = client.get("/invoices/?void=1").text
        self.assertIn("Vic Void", shown)
        self.assertIn('data-owed="900.25"', shown)  # void never counts as owed

    def test_approved_work_without_invoice_is_listed_read_only(self):
        self.seed()
        html = self.sign_in().get("/invoices/").text
        section = html.split('id="not-invoiced"')[1].split('id="paid"')[0]
        self.assertIn(f"/ro/{self.pending_job}'", section)
        self.assertIn("Nia Noinvoice", section)
        self.assertIn("$309.20", section)  # 200 + 100 + 9.20 tax on the part; pending line left out
        self.assertIn("$259.20", section)  # less the $50 deposit
        self.assertEqual(len(re.findall(r"/ro/\d+'", section)), 1)  # the invoiced job isn't repeated
        with self.Session() as db:
            self.assertEqual(db.query(Invoice).count(), 4)  # looking never creates an invoice

    def test_empty_page(self):
        html = self.sign_in().get("/invoices/").text
        self.assertIn("Nothing owed on any invoice.", html)
        self.assertIn('data-owed="0.00"', html)
        self.assertNotIn('id="not-invoiced"', html)


if __name__ == "__main__":
    unittest.main()
