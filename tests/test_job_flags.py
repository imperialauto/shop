"""Flags for jobs that don't add up. Made-up data, private in-memory database."""
import unittest
from datetime import datetime, timedelta, timezone

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import job_flags
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)
RECENT = datetime(2026, 11, 24, 12, 0)  # naive UTC, like the model defaults


class JobFlagTests(unittest.TestCase):
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
            db.add_all([User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"),
                        Customer(id=1, first_name="Pat", last_name="Sample", phone="6025550100"),
                        Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"),
                        Vehicle(id=2, customer_id=1, year=2005, make="Cadillac", model="Escalade"),
                        RepairOrder(id=5, customer_id=1, vehicle_id=1, status="intake", public_token="t" * 20,
                                    created_at=RECENT, updated_at=RECENT)])
            db.commit()

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def change(self, fn, ro_id=5):
        with self.Session() as db:
            fn(db, db.get(RepairOrder, ro_id))
            db.commit()

    def line(self, db, desc, kind="labor", approval=None, price=100.0, created=RECENT, done=None, ro_id=5):
        db.add(LineItem(repair_order_id=ro_id, description=desc, item_type=kind, unit_price=price,
                        approval=approval, created_at=created, completed_at=done))

    def flags(self, ro_id=5):
        with self.Session() as db:
            return job_flags.for_job(db, db.get(RepairOrder, ro_id), now=NOW)

    def codes(self, ro_id=5):
        return [f["code"] for f in self.flags(ro_id)]

    def set_status(self, status):
        self.change(lambda db, ro: setattr(ro, "status", status))
        self.change(lambda db, ro: setattr(ro, "updated_at", RECENT))

    def test_clean_job_has_no_flags(self):
        def setup(db, ro):
            self.line(db, "Replace oil pan gasket", "labor", "approved")
            self.line(db, "Oil pan gasket", "part", "approved")
            ro.status = "in_progress"
            ro.updated_at = RECENT
        self.change(setup)
        self.assertEqual(self.flags(), [])
        with self.Session() as db:
            self.assertEqual(job_flags.open_jobs_with_flags(db, now=NOW), [])

    def test_part_approved_labor_declined(self):
        def setup(db, ro):
            self.line(db, "Replace oil pan gasket", "labor", "declined")
            self.line(db, "Oil pan gasket", "part", "approved")
            self.line(db, "Front brake pads", "part", "approved")
            ro.status = "in_progress"
        self.change(setup)
        flags = [f for f in self.flags() if f["code"] == "part_labor_mismatch"]
        self.assertEqual(len(flags), 1)
        self.assertIn('"Oil pan gasket"', flags[0]["message"])
        self.assertIn('"Replace oil pan gasket"', flags[0]["message"])
        self.assertEqual(flags[0]["severity"], "warn")

    def test_labor_approved_part_declined(self):
        def setup(db, ro):
            self.line(db, "R&R water pump", "labor", "approved")
            self.line(db, "Water pump assembly", "part", "declined")
            ro.status = "in_progress"
        self.change(setup)
        msg = next(f["message"] for f in self.flags() if f["code"] == "part_labor_mismatch")
        self.assertTrue(msg.startswith('The labor "R&R water pump" is approved'))

    def test_unrelated_lines_do_not_match(self):
        def setup(db, ro):
            self.line(db, "Front brake pads", "part", "approved")
            self.line(db, "Rear brake rotors", "labor", "declined")
            ro.status = "in_progress"
        self.change(setup)
        self.assertNotIn("part_labor_mismatch", self.codes())

    def test_waiting_approval_with_nothing_pending(self):
        self.change(lambda db, ro: self.line(db, "Diag", "labor", "approved"))
        self.set_status("waiting_approval")
        self.assertIn("status_waiting_no_pending", self.codes())

    def test_working_statuses_with_nothing_approved(self):
        self.change(lambda db, ro: self.line(db, "Diag", "labor", "declined"))
        for status in ("in_progress", "waiting_parts", "complete"):
            self.set_status(status)
            self.assertIn("status_no_approved", self.codes(), status)

    def test_approval_received_with_pending_lines(self):
        def setup(db, ro):
            self.line(db, "Struts", "labor", "approved")
            self.line(db, "Alignment", "labor", "pending")
        self.change(setup)
        self.set_status("approval_received")
        self.assertIn("status_approved_but_pending", self.codes())

    def test_complete_with_approved_lines_not_done_is_info(self):
        def setup(db, ro):
            self.line(db, "Struts", "labor", "approved", done=datetime(2026, 11, 24, tzinfo=timezone.utc))
            self.line(db, "Alignment", "labor", "approved")
        self.change(setup)
        self.set_status("complete")
        flag = next(f for f in self.flags() if f["code"] == "approved_not_done")
        self.assertEqual(flag["severity"], "info")
        self.assertIn("1 approved line", flag["message"])

    def test_delivered_with_pending_lines(self):
        def setup(db, ro):
            self.line(db, "Struts", "labor", "approved", done=datetime(2026, 11, 24, tzinfo=timezone.utc))
            self.line(db, "Alignment", "labor", "pending", created=datetime(2026, 1, 1))
        self.change(setup)
        self.set_status("delivered")
        codes = self.codes()
        self.assertIn("delivered_with_pending", codes)
        # Picked up is closed: no "waiting too long" or "stale" on it.
        self.assertNotIn("waiting_too_long", codes)

    def test_cancelled_job_has_no_flags(self):
        self.change(lambda db, ro: self.line(db, "Alignment", "labor", "pending", created=datetime(2026, 1, 1)))
        self.set_status("cancelled")
        self.assertEqual(self.flags(), [])

    def test_waiting_on_customer_too_long(self):
        self.change(lambda db, ro: self.line(db, "DPS6 clutch", "labor", "pending",
                                             created=NOW.replace(tzinfo=None) - timedelta(days=142)))
        self.set_status("waiting_approval")
        flag = next(f for f in self.flags() if f["code"] == "waiting_too_long")
        self.assertIn("142 days", flag["message"])
        self.assertIn("close it out", flag["message"])

    def test_recent_pending_is_not_too_long(self):
        self.change(lambda db, ro: self.line(db, "DPS6 clutch", "labor", "pending",
                                             created=NOW.replace(tzinfo=None) - timedelta(days=3)))
        self.set_status("waiting_approval")
        self.assertEqual(self.flags(), [])

    def test_duplicate_open_job_same_customer_and_vehicle(self):
        with self.Session() as db:
            db.add(RepairOrder(id=9, customer_id=1, vehicle_id=1, status="intake", created_at=RECENT, updated_at=RECENT))
            db.add(RepairOrder(id=10, customer_id=1, vehicle_id=2, status="intake", created_at=RECENT, updated_at=RECENT))
            db.commit()
        flag = next(f for f in self.flags() if f["code"] == "duplicate_open_job")
        self.assertIn("RO #9", flag["message"])
        self.assertNotIn("RO #10", flag["message"])
        self.assertNotIn("duplicate_open_job", self.codes(10))
        # Closing the other one clears it.
        self.change(lambda db, ro: setattr(ro, "status", "cancelled"), ro_id=9)
        self.assertNotIn("duplicate_open_job", self.codes())

    def test_duplicate_through_duplicate_records(self):
        with self.Session() as db:
            db.add_all([Customer(id=2, first_name="Pat", last_name="Sample", phone="(602) 555-0100"),
                        Vehicle(id=3, customer_id=2, year=2016, make="ford", model="Focus"),
                        RepairOrder(id=11, customer_id=2, vehicle_id=3, status="diagnosing",
                                    created_at=RECENT, updated_at=RECENT)])
            db.commit()
        self.assertIn("duplicate_open_job", self.codes())

    def test_old_job_with_no_activity(self):
        self.change(lambda db, ro: None)
        with self.Session() as db:
            db.query(RepairOrder).filter_by(id=5).update({"updated_at": datetime(2026, 8, 1)})
            db.commit()
        flag = next(f for f in self.flags() if f["code"] == "stale_job")
        self.assertIn("116 days", flag["message"])

    def test_invoice_total_does_not_match_lines(self):
        def setup(db, ro):
            self.line(db, "Brake job", "labor", "approved", price=200, done=datetime(2026, 11, 24, tzinfo=timezone.utc))
            self.line(db, "Brake pads", "part", "approved", price=100, done=datetime(2026, 11, 24, tzinfo=timezone.utc))
            db.add(Invoice(id=3, repair_order_id=5, subtotal=300, tax_rate=0.092, tax_amount=9.2, total=309.2, status="sent"))
        self.change(setup)
        self.set_status("complete")
        self.assertEqual(self.flags(), [])  # matches
        self.change(lambda db, ro: self.line(db, "Rotors", "part", "approved", price=80,
                                             done=datetime(2026, 11, 24, tzinfo=timezone.utc)))
        flag = next(f for f in self.flags() if f["code"] == "invoice_mismatch")
        self.assertIn("Invoice #3 is for $309.20", flag["message"])
        self.assertIn("$396.56", flag["message"])  # 380 + 180 * 9.2%
        # A void invoice isn't checked.
        self.change(lambda db, ro: setattr(ro.invoices[0], "status", "void"))
        self.assertNotIn("invoice_mismatch", self.codes())

    def test_flags_are_read_only(self):
        self.change(lambda db, ro: self.line(db, "Alignment", "labor", "pending", created=datetime(2026, 1, 1)))
        self.set_status("waiting_approval")
        with self.Session() as db:
            job_flags.for_job(db, db.get(RepairOrder, 5), now=NOW)
            self.assertFalse(db.new or db.dirty or db.deleted)

    def test_open_jobs_with_flags_lists_flagged_jobs(self):
        self.change(lambda db, ro: self.line(db, "Alignment", "labor", "pending", created=datetime(2026, 1, 1)))
        self.set_status("waiting_approval")
        with self.Session() as db:
            db.add(RepairOrder(id=12, customer_id=1, vehicle_id=2, status="intake", created_at=RECENT, updated_at=RECENT))
            db.commit()
            rows = job_flags.open_jobs_with_flags(db, now=NOW)
            self.assertEqual([ro.id for ro, _ in rows], [5])
            html = Environment(loader=FileSystemLoader("app/templates")).get_template("_flags_home.html") \
                .render(home_flags=rows)
        self.assertIn('href="/ro/5"', html)
        self.assertIn("Pat Sample · 2016 Ford Focus", html)
        self.assertIn("hasn&#39;t answered" if "&#39;" in html else "hasn't answered", html)

    def test_job_page_shows_flags(self):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": "owner", "password": PASSWORD})
        page = client.get("/ro/5").text
        self.assertNotIn('id="job-flags"', page)
        self.change(lambda db, ro: self.line(db, "Diag", "labor", "approved"))
        self.change(lambda db, ro: setattr(ro, "status", "waiting_approval"))
        page = client.get("/ro/5").text
        self.assertIn('id="job-flags"', page)
        self.assertIn('data-flag="status_waiting_no_pending"', page)


if __name__ == "__main__":
    unittest.main()
