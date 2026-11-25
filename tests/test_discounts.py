"""Discount lines: added on the job page or by the assistant, shown as money off on the approval page
and invoice, and taken out with everything else when the customer declines it all. Made-up data only."""
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_keys, discounts
from app import business_settings as store
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Communication, Customer, LineItem, RepairOrder, User, Vehicle
from app.routes.invoices import totals_for
from app.security import limiter

PASSWORD = "fixture-password-1"
SIGNATURE = "data:image/png;base64,iVBORw0KGgo="


class DiscountTests(unittest.TestCase):
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
        self.env = patch.dict(os.environ, {"AGENT_API_ENABLED": "true"})
        self.env.start()
        self.read_only = app.state.read_only
        app.state.read_only = False
        limiter.clear("agent:ip:testclient")
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            c = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2015, make="Ford", model="Edge")
            db.add(v)
            db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="intake", public_token="disc-token-1234")
            db.add(ro)
            db.flush()
            for d, kind, q, p in (("Tow hookup", "misc", 1, 95), ("Tow mileage", "misc", 20, 4),
                                  ("Water pump", "part", 1, 500), ("Labor", "labor", 2, 150)):
                db.add(LineItem(repair_order_id=ro.id, description=d, item_type=kind, quantity=q, unit_price=p,
                                taxable=kind == "part"))
            db.commit()
            self.ro_id = ro.id
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        limiter.clear("agent:ip:testclient")
        self.env.stop()
        self.store_patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def lines(self):
        with self.Session() as db:
            return [(li.description, li.item_type, li.unit_price, li.taxable, li.approval)
                    for li in db.query(LineItem).filter(LineItem.repair_order_id == self.ro_id).order_by(LineItem.id)]

    def owner(self):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": "owner", "password": PASSWORD})
        return client

    def headers(self):
        with self.Session() as db:
            _, raw = agent_keys.issue(db, 1, "Claude", list(agent_keys.SCOPES), 90)
        return {"Authorization": f"Bearer {raw}"}

    def test_money_filter_puts_the_minus_first(self):
        self.assertEqual(discounts.money(-75), "-$75.00")
        self.assertEqual(discounts.money(1234.5), "$1,234.50")

    def test_job_page_adds_a_discount_as_money_off(self):
        response = self.owner().post(f"/ro/{self.ro_id}/add-line-item", data={
            "description": "Tow discount", "item_type": "discount", "quantity": "1", "unit_price": "75"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lines()[-1], ("Tow discount", "discount", -75.0, False, "approved"))

    def test_discount_bigger_than_the_job_is_refused(self):
        response = self.owner().post(f"/ro/{self.ro_id}/add-line-item", data={
            "description": "Too much", "item_type": "discount", "quantity": "1", "unit_price": "5000"})
        self.assertIn("line_error=discount_too_big", response.headers["location"])
        self.assertEqual(len(self.lines()), 4)

    def test_assistant_adds_a_discount_and_sends_nothing(self):
        response = self.client.post(f"/internal/agent/v1/jobs/{self.ro_id}/lines", headers=self.headers(),
                                    json=[{"description": "Tow discount", "item_type": "discount", "unit_price": 75,
                                           "taxable": True}])
        self.assertEqual(response.status_code, 201, response.text)
        line = response.json()["lines"][-1]
        self.assertEqual((line["item_type"], line["unit_price"], line["taxable"], line["approval"]),
                         ("discount", -75.0, False, "approved"))
        with self.Session() as db:
            self.assertEqual(db.query(Communication).count(), 0)

    def test_assistant_discount_too_big_is_refused(self):
        response = self.client.post(f"/internal/agent/v1/jobs/{self.ro_id}/lines", headers=self.headers(),
                                    json=[{"description": "Too much", "item_type": "discount", "unit_price": 9000}])
        self.assertEqual(response.status_code, 422)

    def test_approval_page_shows_the_discount_off_the_total_with_no_buttons(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, self.ro_id)
            discounts.add(db, ro, "Tow discount", 75, "owner")
            for li in ro.line_items:
                if li.approval is None:
                    li.approval = "pending"
            db.commit()
            discount_id = next(li.id for li in ro.line_items if li.item_type == "discount")
        page = self.client.get("/sign/ro/disc-token-1234").text
        self.assertIn("Tow discount", page)
        self.assertIn("-$75.00", page)
        self.assertNotIn(f'name="line_{discount_id}"', page)
        # 95 + 80 + 500 + 300 - 75 = 900, plus 9.2% tax on the $500 part = 46.00
        self.assertIn("$946.00", page)

    def test_invoice_totals_take_the_discount_off_before_tax_on_parts(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, self.ro_id)
            discounts.add(db, ro, "Tow discount", 75, "owner")
            db.commit()
            self.assertEqual(totals_for([li for li in ro.line_items if li.billable]), (900.0, 0.092, 46.0, 946.0))

    def test_declining_everything_takes_the_discount_out_too(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, self.ro_id)
            discounts.add(db, ro, "Tow discount", 75, "owner")
            for li in ro.line_items:
                if li.approval is None:
                    li.approval = "pending"
            db.commit()
            pending = [li.id for li in ro.line_items if li.approval == "pending"]
        form = {f"line_{i}": "declined" for i in pending}
        form.update(signature_data=SIGNATURE, approved_by="Pat Sample")
        self.client.post("/sign/ro/disc-token-1234", data=form)
        self.assertTrue(all(row[4] == "declined" for row in self.lines()))

    def test_approving_keeps_the_discount(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, self.ro_id)
            discounts.add(db, ro, "Tow discount", 75, "owner")
            for li in ro.line_items:
                if li.approval is None:
                    li.approval = "pending"
            db.commit()
            pending = [li.id for li in ro.line_items if li.approval == "pending"]
        form = {f"line_{i}": "approved" for i in pending}
        form.update(signature_data=SIGNATURE, approved_by="Pat Sample")
        self.client.post("/sign/ro/disc-token-1234", data=form)
        self.assertTrue(all(row[4] == "approved" for row in self.lines()))


if __name__ == "__main__":
    unittest.main()
