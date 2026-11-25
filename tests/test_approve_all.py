"""Approve all / Decline all and one-tap option choice on the customer's approval page. Made-up data only."""
import re
import subprocess
import shutil
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import approval_options
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, RepairOrder, Vehicle

SIGNATURE = "data:image/png;base64,iVBORw0KGgo="
OPTION_LINES = (
    ("OPTION 1 (Brand A parts): upper control arm", 200), ("OPTION 1 (Brand A parts): lower control arm", 250),
    ("OPTION 2 (Brand B kit, in place of OPTION 1): front end kit", 400),
    ("Labor: replace control arms", 450), ("Shop supplies", 100),
    ("OPTIONAL, oil pan gasket: gasket", 70),
    ("Pick one parts option: OPTION 1 or OPTION 2", 0),
)


class ApproveAllTests(unittest.TestCase):
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
            c = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2012, make="Chevrolet", model="Silverado")
            db.add(v)
            db.flush()
            plain = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="waiting_approval", public_token="plain-token-1234")
            options = RepairOrder(customer_id=c.id, vehicle_id=v.id, status="waiting_approval", public_token="option-token-1234")
            db.add_all([plain, options])
            db.flush()
            for d, p in (("Front brakes", 300), ("Rear brakes", 300), ("Wipers", 30)):
                db.add(LineItem(repair_order_id=plain.id, description=d, item_type="labor", quantity=1, unit_price=p,
                                approval="pending"))
            for d, p in OPTION_LINES:
                db.add(LineItem(repair_order_id=options.id, description=d, item_type="part", quantity=1, unit_price=p,
                                approval="pending"))
            db.commit()
            self.options_id = options.id
        self.customer = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def ids(self, ro_id):
        with self.Session() as db:
            return {li.description: li.id for li in db.query(LineItem).filter(LineItem.repair_order_id == ro_id)}

    def test_plain_estimate_has_approve_all_and_decline_all(self):
        page = self.customer.get("/sign/ro/plain-token-1234").text
        self.assertIn('id="approve-all"', page)
        self.assertIn('id="decline-all"', page)
        self.assertNotIn("Choose Option", page)

    def test_option_estimate_offers_one_tap_per_option(self):
        page = self.customer.get("/sign/ro/option-token-1234").text
        self.assertIn("Choose Option 1 (Brand A parts)", page)
        self.assertIn("Choose Option 2 (Brand B kit, in place of OPTION 1)", page)
        self.assertNotIn('id="approve-all"', page)  # approving both options would be wrong
        self.assertIn('id="decline-all"', page)

    def test_groups_ignore_optional_and_mentions(self):
        class L:
            def __init__(self, i, d, t): self.id, self.description, self.total = i, d, t
        lines = [L(i, d, p) for i, (d, p) in enumerate(OPTION_LINES, 1)]
        got = approval_options.groups(lines)
        self.assertEqual([(g["number"], g["ids"], g["total"]) for g in got], [("1", [1, 2], 450.0), ("2", [3], 400.0)])
        self.assertEqual(approval_options.groups(lines[:2]), [])  # one option alone isn't a choice

    def sign(self, choose):
        ids = self.ids(self.options_id)
        data = {"approved_by": "Pat", "signature_data": SIGNATURE}
        for d, i in ids.items():
            data[f"line_{i}"] = "approved" if choose(d) else "declined"
        return self.customer.post("/sign/ro/option-token-1234", data=data)

    def test_approving_both_options_is_refused(self):
        refused = self.sign(lambda d: True)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("Pick one option", refused.text)
        with self.Session() as db:
            self.assertEqual({li.approval for li in db.query(LineItem).filter(LineItem.repair_order_id == self.options_id)},
                             {"pending"})

    def test_one_option_signs(self):
        done = self.sign(lambda d: not d.startswith("OPTION 1") and not d.startswith("OPTIONAL"))
        self.assertEqual(done.status_code, 200)
        with self.Session() as db:
            got = {li.description: li.approval for li in db.query(LineItem).filter(LineItem.repair_order_id == self.options_id)}
        self.assertEqual(got["OPTION 2 (Brand B kit, in place of OPTION 1): front end kit"], "approved")
        self.assertEqual(got["OPTION 1 (Brand A parts): upper control arm"], "declined")

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_page_script_parses(self):
        page = self.customer.get("/sign/ro/option-token-1234").text
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        for js in scripts:
            out = subprocess.run(["node", "--check", "-"], input=js, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
