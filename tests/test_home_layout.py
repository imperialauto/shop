"""Home, simplified (Nov 25, 2026): one list of cars with the car-here mark and one button each,
a Needs-you list that drops items about finished jobs, honest waiting-on-customer days, and a
customer's second open job on the same car folded into one row. Made-up records only."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings, calendar_feed, shop_board
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.routes import dispatch

PASSWORD = "fixture-password-1"


def ago(**delta):
    return datetime.utcnow() - timedelta(**delta)


class HomeLayoutTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        business_settings.clear_cache()
        with self.Session() as db:
            for uid, name, role in ((1, "owner", "owner"), (2, "office", "office"), (3, "crew", "tech"), (4, "boss", "admin")):
                db.add(User(id=uid, username=name, password_hash=hash_password(PASSWORD), role=role))
            db.add_all([Customer(id=1, first_name="Avery", last_name="Sample", phone="+16025550100"),
                        Customer(id=2, first_name="Robin", last_name="Fixture", phone="+16025550101"),
                        Customer(id=3, first_name="Casey", last_name="Example", phone="+16025550102")])
            db.flush()
            db.add_all([Vehicle(id=1, customer_id=1, year=2009, make="Chevrolet", model="Impala"),
                        Vehicle(id=2, customer_id=2, year=2016, make="Ford", model="Focus"),
                        Vehicle(id=3, customer_id=3, year=2005, make="Cadillac", model="Escalade")])
            db.flush()
            db.add_all([
                RepairOrder(id=1, customer_id=1, vehicle_id=1, status="intake", public_token="t1"),         # older job, same car
                RepairOrder(id=2, customer_id=1, vehicle_id=1, status="diagnosing", public_token="t2"),     # newest: shown
                RepairOrder(id=3, customer_id=2, vehicle_id=2, status="waiting_approval", public_token="t3"),  # nothing pending
                RepairOrder(id=4, customer_id=3, vehicle_id=3, status="waiting_approval", public_token="t4"),  # pending, sent
                RepairOrder(id=5, customer_id=2, vehicle_id=2, status="delivered", public_token="t5"),
            ])
            db.flush()
            db.add(LineItem(repair_order_id=4, description="Fuel pump", unit_price=400, approval="pending",
                            created_at=ago(days=40)))
            db.add(LineItem(repair_order_id=3, description="Clutch", unit_price=900, approval="declined",
                            created_at=ago(days=142)))
            link4 = DispatchItem(quo_conversation_id="LINK4", customer_phone="+16025550102", customer_id=3, status="approved",
                                 category="approval_link", summary="Job #4 link", last_message_id="m4",
                                 decided_at=datetime.now(timezone.utc) - timedelta(days=12))
            db.add(link4)
            # Queue items: one per job state, plus a plain customer text.
            for key, phone, summary in (("LINK5", "+16025550101", "STALE delivered link"),
                                        ("LINK3", "+16025550101", "STALE nothing pending"),
                                        ("PICKUP2", "+16025550100", "LIVE pickup for an open job"),
                                        ("CN9", "+16025550109", "LIVE plain customer text")):
                db.add(DispatchItem(quo_conversation_id=key, customer_phone=phone, status="needs_owner",
                                    category="approval_link" if key.startswith("LINK") else "general",
                                    summary=summary, last_message_id="m-" + key))
            db.flush()
            db.add(DispatchOutbound(dedupe_key="approval:4:m4", kind="dashboard_reply", item_id=link4.id, status="sent",
                                    updated_at=datetime.now(timezone.utc) - timedelta(days=10)))
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.read_only, app.state.read_only = app.state.read_only, False
        self.calendar = patch.object(calendar_feed, "upcoming", return_value=[])
        self.calendar.start()

    def tearDown(self):
        self.calendar.stop()
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only
        business_settings.clear_cache()
        self.engine.dispose()

    def client(self, name="owner"):
        client = TestClient(app)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def test_home_renders_for_each_role(self):
        for name in ("owner", "office", "boss"):
            page = self.client(name).get("/dashboard")
            self.assertEqual(page.status_code, 200, name)
            for heading in ("Needs you", "Cars in the shop", "Waiting on customer", "Coming in", 'id="more"'):
                self.assertIn(heading, page.text)
            self.assertNotIn('aria-label="Need you"', page.text)  # the duplicate count tile is gone
            self.assertEqual("Money in today" in page.text, name == "owner")
        crew = self.client("crew").get("/dashboard", follow_redirects=False)
        self.assertEqual(crew.headers["location"], "/my-day")  # technicians land on My Day

    def test_queue_hides_items_about_finished_jobs(self):
        page = self.client().get("/dashboard").text
        self.assertNotIn("STALE", page)
        self.assertIn("LIVE pickup for an open job", page)
        self.assertIn("LIVE plain customer text", page)
        with self.Session() as db:  # the stored rows are untouched: this is worked out when read
            self.assertEqual(db.query(DispatchItem).filter(DispatchItem.status == "needs_owner").count(), 4)
            db.get(RepairOrder, 2).status = "cancelled"
            db.commit()
        self.assertNotIn("LIVE pickup", self.client().get("/dashboard").text)

    def test_complete_job_whose_car_left_drops_out(self):
        with self.Session() as db:
            db.get(RepairOrder, 2).status = "complete"
            db.commit()
        owner = self.client()
        self.assertIn("LIVE pickup", owner.get("/dashboard").text)
        owner.post("/board/lot/2", data={"operation": "gone", "back": "home"}, headers={"Origin": "http://testserver"})
        self.assertNotIn("LIVE pickup", owner.get("/dashboard").text)

    def test_waiting_on_customer_needs_pending_lines_and_counts_from_the_send(self):
        with self.Session() as db:
            rows = shop_board.waiting_on_customer(db, datetime.now(timezone.utc))
        # Job 3 sits in waiting_approval with nothing pending (the 142-day Focus): not listed.
        self.assertEqual([r["id"] for r in rows], [4])
        self.assertEqual(rows[0]["wait"], "10 days")  # the link went out 10 days ago, not the line's 40
        self.assertFalse(rows[0]["stale"])
        with self.Session() as db:
            db.query(DispatchOutbound).delete()
            db.commit()
            rows = shop_board.waiting_on_customer(db, datetime.now(timezone.utc))
        self.assertEqual(rows[0]["wait"], "12 days")  # no send on record: when the owner approved the text
        with self.Session() as db:
            db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == "LINK4").delete()
            db.commit()
            rows = shop_board.waiting_on_customer(db, datetime.now(timezone.utc))
        self.assertEqual((rows[0]["wait"], rows[0]["stale"]), ("40 days", True))  # newest pending line
        page = self.client().get("/dashboard").text
        self.assertIn("Over a month", page)

    def test_two_open_jobs_on_one_car_show_as_one_row(self):
        page = self.client().get("/dashboard").text
        self.assertIn('id="job-2"', page)
        self.assertNotIn('id="job-1"', page)
        self.assertIn('href="/ro/1"', page)
        self.assertIn("+1 other open job", page)

    def test_car_here_toggle_on_home_and_job_page(self):
        owner = self.client()
        page = owner.get("/dashboard").text
        self.assertIn('action="/board/lot/2"', page)
        self.assertIn("Not here yet", page)
        r = owner.post("/board/lot/4", data={"operation": "here", "back": "home"},
                       headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/dashboard#job-4")
        job = owner.get("/ro/4").text
        self.assertIn('id="car-here"', job)
        self.assertIn("✓ Car here", job)
        r = owner.post("/board/lot/4", data={"operation": "gone", "back": "job"},
                       headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/ro/4")
        self.assertIn("Not here yet", owner.get("/ro/4").text)
        self.assertNotIn('id="car-here"', owner.get("/ro/5").text)  # picked up: no mark

    def test_stale_check_leaves_review_and_oil_items_alone(self):
        with self.Session() as db:
            for key in ("REV5", "OIL5"):
                item = DispatchItem(quo_conversation_id=key, customer_phone="+1", status="needs_owner", last_message_id=key)
                self.assertFalse(dispatch.stale_for_job(db, item))
            self.assertTrue(dispatch.stale_for_job(db, DispatchItem(quo_conversation_id="WEB5-decision", customer_phone="+1",
                                                                    status="needs_owner", last_message_id="w")))


if __name__ == "__main__":
    unittest.main()
