"""Approvals taken in person or by phone, payments taken outside the app, and waived balances.
Made-up data, private database. Nothing here texts anyone."""
import unittest
from datetime import datetime, timedelta

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import approval_links, customer_file
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, User, Vehicle
from app.routes.invoices import _shop_day, paid_on_to_utc

PASSWORD = "fixture-password-1"


class InPersonApprovalTests(unittest.TestCase):
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
            db.add(User(id=3, username="desk", password_hash=hash_password(PASSWORD), role="office"))
            customer = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add(customer)
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2009, make="Chevrolet", model="Impala")
            db.add(vehicle)
            db.flush()
            order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="in_progress", public_token="made-up-token")
            db.add(order)
            db.flush()
            db.add(LineItem(repair_order_id=order.id, description="Estimate work", item_type="labor", quantity=1,
                            unit_price=484.22, approval="approved", approval_source="link"))
            for description, kind, price in (("Headlight labor", "labor", 45), ("H11 bulb", "part", 23.80),
                                             ("Valve covers", "misc", 325)):
                db.add(LineItem(repair_order_id=order.id, description=description, item_type=kind, quantity=1, unit_price=price))
            db.commit()
            self.job = order.id
        self.owner = self.client("owner")

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def client(self, username):
        c = TestClient(app, follow_redirects=False)
        c.post("/login", data={"username": username, "password": PASSWORD})
        return c

    def lines(self):
        with self.Session() as db:
            return {li.description: (li.approval, li.approval_source, li.approval_recorded_by) for li in db.query(LineItem).all()}

    def ids(self, *names):
        with self.Session() as db:
            found = {li.description: li.id for li in db.query(LineItem).all()}
        return [found[n] for n in names]

    def approve(self, client, ids, source="in_person"):
        return client.post(f"/ro/{self.job}/approve-in-person", data={"line_ids": [str(i) for i in ids], "source": source})

    def invoice(self):
        self.assertEqual(self.owner.post(f"/invoices/create/{self.job}").status_code, 302)
        with self.Session() as db:
            return db.query(Invoice).one().id

    # ── Approvals ──────────────────────────────────────────────────────────

    def test_owner_marks_lines_approved_in_person_without_texting(self):
        page = self.owner.get(f"/ro/{self.job}").text
        self.assertIn("Customer already said yes in person or by phone?", page)
        response = self.approve(self.owner, self.ids("Headlight labor", "H11 bulb", "Valve covers"))
        self.assertEqual(response.status_code, 303)
        self.assertIn("approved_in_person=3", response.headers["location"])
        lines = self.lines()
        for name in ("Headlight labor", "H11 bulb", "Valve covers"):
            self.assertEqual(lines[name], ("approved", "in_person", "owner"))
        self.assertEqual(lines["Estimate work"], ("approved", "link", None))
        page = self.owner.get(f"/ro/{self.job}").text
        self.assertIn("Approved in person · recorded by owner", page)
        self.assertNotIn("Ask customer to approve", page)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).count(), 0)  # nothing queued, nothing sent

    def test_phone_approval_and_history_wording(self):
        self.approve(self.client("desk"), self.ids("Valve covers"), source="phone")
        self.assertEqual(self.lines()["Valve covers"], ("approved", "phone", "desk"))
        with self.Session() as db:
            ro = db.get(RepairOrder, self.job)
            titles = [e["title"] for e in customer_file.build(db, ro.customer, fetch=lambda *a: ({}, None))["entries"]]
        self.assertTrue(any("approved by phone" in t and "recorded by desk" in t for t in titles), titles)

    def test_pending_lines_close_the_queued_approval_text(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, self.job)
            for li in ro.line_items:
                if li.approval is None:
                    li.approval = "pending"
            ro.status = "waiting_approval"
            approval_links.queue(db, ro, "none", "owner")
            db.commit()
        self.approve(self.owner, self.ids("Headlight labor", "H11 bulb"))
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).one().status, "needs_owner")  # valve covers still waiting
        self.approve(self.owner, self.ids("Valve covers"))
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).one().status, "closed")
            self.assertEqual(db.get(RepairOrder, self.job).status, "approval_received")

    def test_bad_requests_change_nothing(self):
        before = self.lines()
        self.assertIn("approve_error=how", self.approve(self.owner, self.ids("Valve covers"), source="email").headers["location"])
        self.assertIn("approve_error=lines", self.approve(self.owner, []).headers["location"])
        self.assertIn("approve_error=lines", self.approve(self.owner, self.ids("Estimate work")).headers["location"])
        self.assertIn("approve_error=lines", self.approve(self.owner, [999999]).headers["location"])
        self.assertEqual(self.lines(), before)

    def test_tech_cannot_record_approvals(self):
        self.assertEqual(self.approve(self.client("crew"), self.ids("Valve covers")).status_code, 403)
        self.assertEqual(self.lines()["Valve covers"][0], None)

    def test_invoiced_job_refuses_lines_left_off_the_invoice(self):
        with self.Session() as db:
            db.query(LineItem).filter(LineItem.description == "Valve covers").one().approval = "declined"
            db.commit()
        self.invoice()
        self.assertIn("approve_error=invoiced", self.approve(self.owner, self.ids("Valve covers")).headers["location"])
        self.assertEqual(self.lines()["Valve covers"][0], "declined")
        # Lines already on the invoice can still get their record.
        self.assertIn("approved_in_person=1", self.approve(self.owner, self.ids("Headlight labor")).headers["location"])

    # ── Payments and waivers ───────────────────────────────────────────────

    def test_full_close_out_with_back_dated_payments_and_waiver(self):
        self.approve(self.owner, self.ids("Headlight labor", "H11 bulb", "Valve covers"))
        with self.Session() as db:
            db.get(RepairOrder, self.job).status = "complete"
            db.commit()
        invoice_id = self.invoice()
        with self.Session() as db:
            inv = db.get(Invoice, invoice_id)
            self.assertAlmostEqual(inv.subtotal, 878.02, places=2)
            self.assertEqual(inv.tax_amount, 2.19)  # bulb is a part, taxed at the shop rate
            total = round(inv.total, 2)
        today = _shop_day(datetime.utcnow())
        for amount, days_ago in ((200, 5), (302, 1), (300, 0)):
            r = self.owner.post(f"/invoices/{invoice_id}/add-payment", data={
                "amount": amount, "method": "zelle", "paid_on": (today - timedelta(days=days_ago)).isoformat()})
            self.assertEqual(r.status_code, 302, r.headers.get("location"))
        with self.Session() as db:
            inv = db.get(Invoice, invoice_id)
            self.assertEqual(inv.status, "sent")
            self.assertEqual(sorted(_shop_day(p.paid_at) for p in inv.payments),
                             [today - timedelta(days=5), today - timedelta(days=1), today])
            left = inv.balance
        self.assertEqual(left, round(total - 802, 2))
        r = self.owner.post(f"/invoices/{invoice_id}/waive", data={"amount": left, "reason": "Courtesy", "picked_up": "on"})
        self.assertEqual(r.status_code, 302, r.headers.get("location"))
        with self.Session() as db:
            inv = db.get(Invoice, invoice_id)
            self.assertEqual((inv.status, inv.balance, inv.waived_by, inv.waived_reason), ("paid", 0, "owner", "Courtesy"))
            self.assertEqual(db.get(RepairOrder, self.job).status, "delivered")
            self.assertEqual(round(sum(p.amount for p in db.query(Payment).all()), 2), 802)  # waiver is not money in
        page = self.owner.get(f"/invoices/{invoice_id}").text
        self.assertIn(f"Waived: <span class=\"text-stone-200\">${left:.2f}", page)
        self.assertNotIn("Record Payment", page)

        self.assertEqual(self.owner.post(f"/invoices/{invoice_id}/waive/undo").status_code, 302)
        with self.Session() as db:
            inv = db.get(Invoice, invoice_id)
            self.assertEqual((inv.status, inv.balance, inv.waived_amount), ("sent", left, 0))

    def test_waive_rules(self):
        self.approve(self.owner, self.ids("Headlight labor", "H11 bulb", "Valve covers"))
        invoice_id = self.invoice()
        desk = self.client("desk")
        self.assertEqual(desk.post(f"/invoices/{invoice_id}/waive", data={"amount": 10}).status_code, 403)
        for amount in (0, -5, 10.005, 5000):
            r = self.owner.post(f"/invoices/{invoice_id}/waive", data={"amount": amount})
            self.assertIn("error=", r.headers["location"], amount)
        with self.Session() as db:
            self.assertEqual(db.get(Invoice, invoice_id).waived_amount, 0)
        self.owner.post(f"/invoices/{invoice_id}/waive", data={"amount": 50})
        with self.Session() as db:
            inv = db.get(Invoice, invoice_id)
            self.assertEqual((inv.waived_amount, inv.status), (50, "draft"))
            balance = inv.balance
        # A payment can't go over what is left after the waiver.
        r = self.owner.post(f"/invoices/{invoice_id}/add-payment", data={"amount": balance + 1, "method": "cash"})
        self.assertIn("balance", r.headers["location"])
        self.assertNotIn("Waive the rest", desk.get(f"/invoices/{invoice_id}").text)

    def test_payment_dates(self):
        now = datetime(2026, 11, 20, 23, 0)  # 4 PM in Glendale
        self.assertEqual(paid_on_to_utc("", now), (None, None))
        self.assertEqual(paid_on_to_utc("2026-11-20", now), (None, None))
        self.assertEqual(paid_on_to_utc("2026-11-19", now), (datetime(2026, 11, 19, 19, 0), None))
        self.assertIn("future", paid_on_to_utc("2026-11-21", now)[1])
        self.assertIn("year", paid_on_to_utc("2025-01-01", now)[1])
        self.assertIn("date", paid_on_to_utc("19/11/2026", now)[1])
        # 11:30 PM UTC on the 20th is still the 20th in Glendale (UTC-7), not the 21st.
        self.assertEqual(_shop_day(datetime(2026, 11, 21, 5, 0)), datetime(2026, 11, 20).date())

    def test_same_day_back_dated_duplicate_is_refused(self):
        invoice_id = self.invoice()
        day = (_shop_day(datetime.utcnow()) - timedelta(days=2)).isoformat()
        self.owner.post(f"/invoices/{invoice_id}/add-payment", data={"amount": 100, "method": "zelle", "paid_on": day})
        again = self.owner.post(f"/invoices/{invoice_id}/add-payment", data={"amount": 100, "method": "zelle", "paid_on": day})
        self.assertIn("already%20recorded%20for%20that%20day", again.headers["location"])
        ok = self.owner.post(f"/invoices/{invoice_id}/add-payment",
                             data={"amount": 100, "method": "zelle", "paid_on": day, "reference": "second"})
        self.assertEqual(ok.headers["location"], f"/invoices/{invoice_id}")
        with self.Session() as db:
            self.assertEqual(db.query(Payment).count(), 2)


if __name__ == "__main__":
    unittest.main()
