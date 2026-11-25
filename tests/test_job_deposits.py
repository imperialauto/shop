"""Deposits recorded on a job before it has an invoice. Made-up data only."""
import json
import unittest
from datetime import datetime, timedelta

from test_agent_api import ALL, _Setup

from app import job_deposits, owner_sheet
from app.agent_keys import AgentAction
from app.models import Customer, Invoice, JobDeposit, LineItem, Payment, RepairOrder, User, Vehicle
from app.auth import hash_password
from test_agent_api import PASSWORD


class JobDepositTests(_Setup):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            db.add(User(id=3, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            c = Customer(first_name="Pat", last_name="Example", phone="6025550100")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2013, make="Ford", model="Fusion")
            db.add(v)
            db.flush()
            ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="approval_received", public_token="d" * 40)
            db.add(ro)
            db.flush()
            # $300 part (taxed 9.2%) + $350 labor = $677.60; half is $338.80
            db.add_all([
                LineItem(repair_order_id=ro.id, description="Fuel tank (used)", item_type="part", quantity=1,
                         unit_price=300, approval="approved"),
                LineItem(repair_order_id=ro.id, description="Replace tank", item_type="labor", quantity=1,
                         unit_price=350, approval="approved"),
                LineItem(repair_order_id=ro.id, description="Wipers", item_type="part", quantity=1,
                         unit_price=40, approval="declined"),
            ])
            db.commit()
            self.ro_id = ro.id
        self.owner = self.sign_in("owner")

    def record(self, client=None, **form):
        data = {"amount": "325", "method": "zelle", "reference": "", "paid_on": ""}
        data.update(form)
        return (client or self.owner).post(f"/ro/{self.ro_id}/deposits", data=data)

    def deposits(self):
        with self.Session() as db:
            return [(d.amount, d.method, d.payment_id is not None, d.removed_at is not None)
                    for d in db.query(JobDeposit).order_by(JobDeposit.id)]

    def test_owner_records_a_zelle_deposit_and_the_page_shows_it(self):
        response = self.record(reference="ZL-0001")
        self.assertEqual(response.status_code, 303)
        self.assertIn("deposit=recorded", response.headers["location"])
        self.assertEqual(self.deposits(), [(325.0, "zelle", False, False)])
        page = self.owner.get(f"/ro/{self.ro_id}").text
        self.assertIn("$325.00 · Zelle", page)
        self.assertIn("$13.80 short of 50%", page)
        with self.Session() as db:
            self.assertEqual(db.query(Payment).count(), 0)  # no invoice yet, nothing invented

    def test_backdated_deposit_keeps_its_day(self):
        from app.routes.invoices import _shop_day
        yesterday = (_shop_day(datetime.utcnow()) - timedelta(days=1)).isoformat()  # shop day, not UTC day
        self.assertEqual(self.record(paid_on=yesterday).status_code, 303)
        with self.Session() as db:
            dep = db.query(JobDeposit).one()
            self.assertEqual(dep.received_at.hour, 19)  # noon in Phoenix

    def test_bad_entries_are_refused(self):
        for form in ({"amount": "0"}, {"amount": "10.005"}, {"method": "bitcoin"}, {"amount": "5000"},
                     {"paid_on": "2999-01-01"}):
            response = self.record(**form)
            self.assertIn("deposit_error=", response.headers["location"], form)
        self.assertEqual(self.deposits(), [])

    def test_same_deposit_twice_needs_a_reference(self):
        self.record()
        self.assertIn("deposit_error=", self.record().headers["location"])
        self.assertIn("deposit=recorded", self.record(reference="second").headers["location"])
        self.assertEqual(len(self.deposits()), 2)

    def test_tech_cannot_record_and_does_not_see_the_box(self):
        tech = self.sign_in("tech")
        self.assertEqual(self.record(client=tech).status_code, 403)
        self.assertNotIn("Record deposit", tech.get(f"/ro/{self.ro_id}").text)
        office = self.sign_in("office")
        self.assertEqual(self.record(client=office).status_code, 303)

    def test_creating_the_invoice_carries_the_deposit_over(self):
        self.record(reference="ZL-0001")
        response = self.owner.post(f"/invoices/create/{self.ro_id}")
        self.assertEqual(response.status_code, 302)
        with self.Session() as db:
            invoice = db.query(Invoice).one()
            self.assertEqual(invoice.status, "draft")  # still a draft: nothing was sent
            self.assertEqual(invoice.paid_total, 325.0)
            pay = db.query(Payment).one()
            self.assertEqual((pay.method, pay.amount), ("zelle", 325.0))
            self.assertIn("ZL-0001", pay.reference)
        self.assertEqual(self.deposits(), [(325.0, "zelle", True, False)])

    def test_deposit_after_the_invoice_goes_straight_onto_it(self):
        self.owner.post(f"/invoices/create/{self.ro_id}")
        self.record()
        with self.Session() as db:
            self.assertEqual(db.query(Invoice).one().paid_total, 325.0)
            self.assertEqual(db.query(Payment).count(), 1)

    def test_only_owner_removes_and_only_before_the_invoice(self):
        self.record()
        with self.Session() as db:
            dep_id = db.query(JobDeposit).one().id
        office = self.sign_in("office")
        self.assertEqual(office.post(f"/ro/{self.ro_id}/deposits/{dep_id}/remove").status_code, 403)
        self.assertIn("deposit=removed",
                      self.owner.post(f"/ro/{self.ro_id}/deposits/{dep_id}/remove").headers["location"])
        self.assertEqual(self.deposits(), [(325.0, "zelle", False, True)])
        self.record(reference="again")
        self.owner.post(f"/invoices/create/{self.ro_id}")
        with self.Session() as db:
            applied = db.query(JobDeposit).filter(JobDeposit.removed_at.is_(None)).one().id
            self.assertEqual(db.query(Invoice).one().paid_total, 325.0)  # removed one never carried over
        refused = self.owner.post(f"/ro/{self.ro_id}/deposits/{applied}/remove")
        self.assertIn("deposit_error=", refused.headers["location"])

    def test_owner_sheet_counts_the_deposit(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, self.ro_id)
            before = owner_sheet.build(db, ro)
            self.assertTrue(any("no 50% deposit" in f for f in before["job_flags"]))
        self.record(amount="338.80")
        with self.Session() as db:
            sheet = owner_sheet.build(db, db.get(RepairOrder, self.ro_id))
            self.assertEqual(sheet["paid"], 338.80)
            self.assertFalse(any("deposit" in f.lower() for f in sheet["job_flags"]))

    def test_record_deposit_tool_logs_and_sends_nothing(self):
        _, headers = self.issue()
        response = self.client.post(f"/internal/agent/v1/jobs/{self.ro_id}/deposits", headers=headers,
                                    json={"amount": 325, "method": "zelle", "reference": "ZL-0001"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["deposit"]["amount"], 325)
        self.assertEqual(body["short"], 13.80)
        again = self.client.post(f"/internal/agent/v1/jobs/{self.ro_id}/deposits", headers=headers,
                                 json={"amount": 325, "method": "zelle", "reference": "ZL-0001"})
        self.assertEqual(again.status_code, 409)
        with self.Session() as db:
            self.assertEqual([a.action for a in db.query(AgentAction).all()], ["job.deposit"])
            from app.models import Communication
            self.assertEqual(db.query(Communication).count(), 0)


if __name__ == "__main__":
    unittest.main()
