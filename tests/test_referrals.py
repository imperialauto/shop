"""$25 + $25 referral and store credit. Made-up data, private database, no live services."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import referrals
from app.database import Base
from app.models import Customer, Invoice, LineItem, Payment, RepairOrder, Vehicle
from app.referral_model import CustomerCredit, Referral


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)
        p = patch.object(referrals, "enabled", lambda: True)
        p.start()
        self.addCleanup(p.stop)
        self.pat = Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100")
        self.lee = Customer(id=2, first_name="Lee", last_name="Sample", phone="6025550101")
        self.db.add_all([self.pat, self.lee, Vehicle(id=1, customer_id=1, make="Ford"), Vehicle(id=2, customer_id=2, make="Kia")])
        self.db.commit()

    def job(self, customer_id, *lines, id=None):
        ro = RepairOrder(id=id, customer_id=customer_id, vehicle_id=customer_id, status="complete")
        self.db.add(ro)
        self.db.flush()
        for kind, price in lines:
            self.db.add(LineItem(repair_order_id=ro.id, description=kind, item_type=kind, unit_price=price, approval="approved"))
        self.db.commit()
        self.db.refresh(ro)
        return ro

    def invoice(self, ro, pay=True):
        added = referrals.apply_to_job(self.db, ro, "office")
        billable = [li for li in ro.line_items if li.billable]
        subtotal = sum(li.total for li in billable)
        tax = round(sum(li.total for li in billable if li.item_type == "part") * 0.092, 2)
        inv = Invoice(repair_order_id=ro.id, subtotal=subtotal, tax_amount=tax, total=subtotal + tax, status="sent")
        self.db.add(inv)
        self.db.flush()
        if pay:
            inv.payments.append(Payment(amount=inv.total, method="zelle"))
            inv.status = "paid"
            referrals.on_paid(self.db, inv)
        self.db.commit()
        return inv, added

    def test_code_is_name_and_four_digits_and_stable(self):
        code = referrals.code_for(self.db, self.pat, "office")
        self.assertRegex(code, r"^PAT\d{4}$")
        self.assertEqual(referrals.code_for(self.db, self.pat, "office"), code)

    def test_friend_gets_25_off_and_referrer_earns_25_credit_once_paid(self):
        code = referrals.code_for(self.db, self.pat, "office")
        referral, problem = referrals.record(self.db, self.lee, code.lower(), "office")
        self.assertIsNone(problem)
        ro = self.job(2, ("labor", 180), ("part", 60))
        inv, added = self.invoice(ro, pay=False)
        self.assertEqual(added, [referrals.REFERRAL_LINE])
        self.assertEqual(inv.subtotal, 215)
        self.assertEqual(inv.tax_amount, 5.52)  # tax stays on parts only
        self.assertEqual(referrals.credit_balance(self.db, 1), 0)  # not until it's paid
        inv.payments.append(Payment(amount=inv.total, method="cash"))
        inv.status = "paid"
        referrals.on_paid(self.db, inv)
        referrals.on_paid(self.db, inv)  # recorded twice: still one credit
        self.db.commit()
        self.assertEqual(referrals.credit_balance(self.db, 1), 25)
        self.assertEqual(self.db.get(Referral, referral.id).status, "earned")

    def test_job_of_200_or_less_gets_no_discount(self):
        referrals.record(self.db, self.lee, referrals.code_for(self.db, self.pat, "office"), "office")
        _, added = self.invoice(self.job(2, ("labor", 200)))
        self.assertEqual(added, [])
        self.assertEqual(referrals.credit_balance(self.db, 1), 0)

    def test_credit_comes_off_the_next_job_and_never_below_zero(self):
        self.db.add(CustomerCredit(customer_id=1, amount=25, kind="referral", actor="system"))
        self.db.commit()
        inv, added = self.invoice(self.job(1, ("labor", 15)))
        self.assertEqual(added, ["Store credit $15.00"])
        self.assertEqual(inv.total, 0)
        self.assertEqual(referrals.credit_balance(self.db, 1), 10)
        _, added = self.invoice(self.job(1, ("labor", 100)))
        self.assertEqual(added, ["Store credit $10.00"])
        self.assertEqual(referrals.credit_balance(self.db, 1), 0)

    def test_rules_on_who_can_be_referred(self):
        code = referrals.code_for(self.db, self.pat, "office")
        self.assertIn("own", referrals.record(self.db, self.pat, code, "office")[1])
        self.assertIn("wasn't found", referrals.record(self.db, self.lee, "NOPE1234", "office")[1])
        self.invoice(self.job(2, ("labor", 50)))
        self.assertIn("new customers", referrals.record(self.db, self.lee, code, "office")[1])

    def test_one_offer_per_job(self):
        referrals.record(self.db, self.lee, referrals.code_for(self.db, self.pat, "office"), "office")
        ro = self.job(2, ("labor", 300), ("discount", -30))
        _, added = self.invoice(ro)
        self.assertEqual(added, [])

    def test_off_does_nothing(self):
        with patch.object(referrals, "enabled", lambda: False):
            self.db.add(CustomerCredit(customer_id=1, amount=25, kind="referral", actor="system"))
            self.db.commit()
            self.assertEqual(referrals.apply_to_job(self.db, self.job(1, ("labor", 300)), "office"), [])


class ReferralPageTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.auth import hash_password
        from app.database import get_db
        from app.main import app
        from app.models import User

        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([User(id=1, username="owner", password_hash=hash_password("fixture-password-1"), role="owner"),
                        Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                        Customer(id=2, first_name="Lee", last_name="Sample", phone="6025550101")])
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(setattr, app.state, "read_only", app.state.read_only)
        app.state.read_only = False
        self.addCleanup(self.engine.dispose)
        for p in (patch.object(referrals, "enabled", lambda: True),
                  patch("app.customer_file._bridge", lambda path, phone: (None, "not_connected"))):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"username": "owner", "password": "fixture-password-1"})

    def test_make_code_and_record_a_referral_from_the_customer_page(self):
        self.assertIn("Make referral code", self.client.get("/customers/1").text)
        self.assertEqual(self.client.post("/customers/1/referral-code").status_code, 303)
        with self.Session() as db:
            code = db.query(referrals.ReferralCode).one().code
        self.assertIn(code, self.client.get("/customers/1").text)
        bad = self.client.post("/customers/2/referred-by", data={"code": "XYZ0000"})
        self.assertIn("referral_error", bad.headers["location"])
        self.client.post("/customers/2/referred-by", data={"code": code})
        self.assertIn("Referred by Pat", self.client.get("/customers/2").text)
