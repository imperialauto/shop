"""Owner-only estimate profit sheet: margins, deposit flag, part sourcing, and nobody else can open it."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401 - local test database only
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import owner_sheet
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, PartsOrder, RepairOrder, User, Vehicle
from app.routes import owner_estimates


class OwnerSheetTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([
                User(id=1, username="owner", password_hash="fixture", role="owner"),
                User(id=2, username="office", password_hash="fixture", role="office"),
                User(id=3, username="admin", password_hash="fixture", role="admin"),
                Customer(id=1, first_name="Sample", last_name="Customer", phone="6025550100"),
            ])
            db.flush()
            db.add(Vehicle(id=1, customer_id=1, year=2012, make="Chevrolet", model="Impala"))
            db.flush()
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="approval_received"))
            db.flush()
            db.add_all([
                LineItem(id=1, repair_order_id=1, description="Oil pan gasket R&R", item_type="labor",
                         quantity=3.0, unit_price=150.0, approval="approved"),
                LineItem(id=2, repair_order_id=1, description="Part: Oil pan gasket", item_type="part",
                         quantity=1, unit_price=65.99, approval="approved"),
                LineItem(id=3, repair_order_id=1, description="Part: 5W-30 oil 5 qt", item_type="part",
                         quantity=1, unit_price=48.48, approval="approved"),
                LineItem(id=4, repair_order_id=1, description="Shop supplies", item_type="misc",
                         quantity=1, unit_price=45.0, taxable=False, approval="approved"),
                LineItem(id=5, repair_order_id=1, description="OPTIONAL, power steering: Part: PS pump",
                         item_type="part", quantity=1, unit_price=120.0, approval="pending"),
                LineItem(id=6, repair_order_id=1, description="Rear brakes", item_type="labor",
                         quantity=1.5, unit_price=150.0, approval="declined"),
            ])
            db.add(PartsOrder(repair_order_id=1, description="Oil pan gasket — anticipated pickup today (unconfirmed)",
                              cost=47.69, quantity=1, status="sourcing", vendor="AutoZone store, 2 in stock"))
            db.commit()

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.old_read_only = app.state.read_only
        app.state.read_only = False
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.old_read_only
        self.engine.dispose()

    def as_user(self, user_id):
        return patch.object(owner_estimates, "get_current_user",
                            side_effect=lambda request, db: db.get(User, user_id))

    def test_numbers(self):
        with self.Session() as db:
            s = owner_sheet.build(db, db.get(RepairOrder, 1))
        main = s["main"]
        self.assertEqual(main.revenue, 450 + 65.99 + 48.48 + 45)
        self.assertEqual(main.cost, 47.69)  # oil has no cost yet
        self.assertEqual(main.missing_costs, 1)
        self.assertEqual(main.hours, 3.0)
        self.assertAlmostEqual(main.tax, round((65.99 + 48.48) * owner_sheet.tax_rate(), 2))
        self.assertEqual(s["optional"].revenue, 120.0)
        self.assertEqual(s["declined"].revenue, 225.0)
        self.assertIn("supplies", main.by_bucket)
        self.assertTrue(any("no 50% deposit" in f for f in s["job_flags"]))
        self.assertTrue(any("no cost recorded" in f for f in s["job_flags"]))

    def test_low_rate_and_below_cost_flags(self):
        with self.Session() as db:
            db.add(LineItem(id=7, repair_order_id=1, description="Discounted labor", item_type="labor",
                            quantity=1, unit_price=135.99, approval="approved"))
            db.add(LineItem(id=8, repair_order_id=1, description="Part: Filter", item_type="part",
                            quantity=2, unit_price=5.0, approval="approved"))
            db.add(PartsOrder(repair_order_id=1, description="Filter", cost=6.0, quantity=1, status="sourcing"))
            db.commit()
            s = owner_sheet.build(db, db.get(RepairOrder, 1))
        flags = {ln.item.id: ln.flags for ln in s["main"].lines}
        self.assertTrue(any("under the $150" in f for f in flags[7]))
        self.assertIn("Sold at or below our cost", flags[8])

    def test_owner_sees_sheet_and_saves_cost_and_store(self):
        with self.as_user(1):
            page = self.client.get("/finance/jobs/1")
            self.assertEqual(page.status_code, 200, page.text[:300])
            self.assertIn("owner sheet", page.text)
            self.assertIn("AutoZone store, 2 in stock", page.text)
            saved = self.client.post("/finance/jobs/1/lines/3/source",
                                     data={"cost": "$38.10", "part_number": "MOB-5W30", "where": "AutoZone, 4 in stock"},
                                     follow_redirects=False)
            self.assertEqual(saved.status_code, 303)
            listing = self.client.get("/finance/jobs")
            self.assertEqual(listing.status_code, 200)
            self.assertIn("#1 Sample Customer", listing.text)
        with self.Session() as db:
            rec = [p for p in db.query(PartsOrder).all() if owner_sheet.part_key(p.description) == "5w-30 oil 5 qt"]
            self.assertEqual(len(rec), 1)
            self.assertEqual((rec[0].cost, rec[0].part_number, rec[0].vendor, rec[0].status),
                             (38.10, "MOB-5W30", "AutoZone, 4 in stock", "sourcing"))
            s = owner_sheet.build(db, db.get(RepairOrder, 1))
            self.assertEqual(s["main"].missing_costs, 0)

    def test_saving_cost_on_a_labor_line_is_refused(self):
        with self.as_user(1):
            r = self.client.post("/finance/jobs/1/lines/1/source", data={"cost": "10"}, follow_redirects=False)
        self.assertEqual(r.status_code, 404)

    def test_bad_cost_is_refused(self):
        with self.as_user(1):
            r = self.client.post("/finance/jobs/1/lines/2/source", data={"cost": "abc"}, follow_redirects=False)
        self.assertEqual(r.status_code, 400)

    def test_read_only_mode_blocks_saving(self):
        app.state.read_only = True
        with self.as_user(1):
            r = self.client.post("/finance/jobs/1/lines/2/source", data={"cost": "40"}, follow_redirects=False)
        self.assertEqual(r.status_code, 503)

    def test_office_and_admin_cannot_open(self):
        for uid in (2, 3):
            with self.as_user(uid):
                self.assertEqual(self.client.get("/finance/jobs/1").status_code, 403)


if __name__ == "__main__":
    unittest.main()
