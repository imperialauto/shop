"""Review text, referral and next-visit offer together, through the dashboard pages. Made-up data, private database."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import next_visit, referrals, review_requests
from app.database import Base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Customer, Invoice, LineItem, RepairOrder, Vehicle
from app.referral_model import CustomerCredit


class OffersStory(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
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
                        Customer(id=2, first_name="Lee", last_name="Sample", phone="6025550101"),
                        Vehicle(id=1, customer_id=1, make="Ford", model="F-150", year=2015),
                        Vehicle(id=2, customer_id=2, make="Kia", model="Soul", year=2018)])
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(setattr, app.state, "read_only", app.state.read_only)
        app.state.read_only = False
        for p in (patch.object(referrals, "enabled", lambda: True), patch.object(next_visit, "enabled", lambda: True),
                  patch.object(review_requests, "enabled", lambda: True),
                  patch("app.customer_file._bridge", lambda path, phone: (None, "not_connected"))):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"username": "owner", "password": "fixture-password-1"})

    def job(self, cid, labor, part=0):
        with self.Session() as db:
            ro = RepairOrder(customer_id=cid, vehicle_id=cid, status="complete", public_token=f"token-for-tests-{cid}-{labor}")
            db.add(ro)
            db.flush()
            db.add(LineItem(repair_order_id=ro.id, description="Labor", item_type="labor", unit_price=labor, approval="approved"))
            if part:
                db.add(LineItem(repair_order_id=ro.id, description="Part", item_type="part", unit_price=part, approval="approved"))
            db.commit()
            return ro.id

    def bill(self, ro_id, pay=True):
        r = self.client.post(f"/invoices/create/{ro_id}")
        self.assertIn(r.status_code, (302, 303), r.text[:300])
        with self.Session() as db:
            inv = db.query(Invoice).filter_by(repair_order_id=ro_id).one()
            lines = [(li.description, li.total) for li in db.get(RepairOrder, ro_id).line_items if li.item_type == "discount"]
            total, iid = inv.total, inv.id
        if pay:
            r = self.client.post(f"/invoices/{iid}/add-payment", data={"amount": f"{total:.2f}", "method": "zelle", "reference": "z1", "picked_up": "on"})
            self.assertIn(r.status_code, (302, 303), r.text[:300])
        return iid, total, lines

    def test_story(self):
        out = {}
        a = self.job(1, 300, 100)
        iid, total, lines = self.bill(a)
        out["pat job1"] = (total, lines)
        with self.Session() as db:
            out["pat job1 status"] = db.get(RepairOrder, a).status
            out["review rows"] = [(r.dedupe_key, r.status) for r in db.query(DispatchOutbound).filter_by(kind="review_request")]
        page = self.client.get(f"/sign/invoice/{iid}?token=token-for-tests-1-300")
        out["receipt offer"] = (page.status_code, "10% off" in page.text)
        with self.Session() as db:
            inv = db.get(Invoice, iid)
            out["earned"] = (inv.status, [(p.amount, p.paid_at) for p in inv.payments], next_visit.earned_by(inv))
        with self.Session() as db:
            code = db.query(referrals.ReferralCode).one().code
        receipt = self.client.get(f"/sign/invoice/{iid}?token=token-for-tests-1-300").text
        out["receipt shows code"] = code in receipt
        self.client.post("/customers/2/referred-by", data={"code": code})
        b = self.job(2, 300)
        _, total_b, lines_b = self.bill(b)
        out["lee job"] = (total_b, lines_b)
        with self.Session() as db:
            out["pat credit"] = sum(c.amount for c in db.query(CustomerCredit).filter_by(customer_id=1))
        c = self.job(1, 500)
        _, total_c, lines_c = self.bill(c)
        out["pat job2"] = (total_c, lines_c)
        d = self.job(1, 60)  # small job right after
        _, total_d, lines_d = self.bill(d)
        out["pat small job"] = (total_d, lines_d)
        cust = self.client.get("/customers/1")
        out["customer page"] = cust.status_code
        out["settings page"] = self.client.get("/settings/business").status_code
        # next day, during shop hours: review texts
        with self.Session() as db:
            tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
            for _ in range(3):
                review_requests.due(db, tomorrow, fetch=lambda path, phone: ({"messages": []}, None), allowed=lambda now: True)
            db.commit()
            out["review queue"] = [(i.customer_phone, (i.draft_reply or "")[:60]) for i in db.query(DispatchItem).all()]
            out["review rows after"] = [(r.dedupe_key, r.status) for r in db.query(DispatchOutbound).filter_by(kind="review_request")]
        self.assertEqual(out["pat job1"], (409.2, []))
        self.assertEqual(out["pat job1 status"], "delivered")
        self.assertEqual(out["review rows"], [("review:1:1", "queued")])
        self.assertEqual(out["receipt offer"][0], 200)
        self.assertTrue(out["receipt offer"][1])
        self.assertTrue(out["receipt shows code"])
        self.assertEqual(out["lee job"], (275.0, [("Referral discount: $25 off your first visit", -25.0)]))
        self.assertEqual(out["pat credit"], 25.0)
        self.assertEqual(out["pat job2"], (425.0, [("Next-visit offer: 10% off labor (from job #1)", -50.0), ("Store credit", -25.0)]))
        self.assertEqual(out["pat small job"], (60.0, []))
        self.assertEqual((out["customer page"], out["settings page"]), (200, 200))
        self.assertEqual(len(out["review queue"]), 2)  # one per customer, ever
        self.assertEqual(sorted(t[:7] for _, t in out["review queue"]), ["Hey Lee", "Hey Pat"])


if __name__ == "__main__":
    unittest.main()
