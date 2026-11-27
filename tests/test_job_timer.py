"""Job clock on the job page: each worker clocks in and out, clocked hours against billed labor hours."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings, job_timer, shop_board
from app.appointments import PHOENIX
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=PHOENIX)


class JobTimerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.env = patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "false"})
        self.env.start()
        self.clock = patch.object(shop_board, "now_local", return_value=NOW)
        self.clock.start()
        self.read_only, app.state.read_only = app.state.read_only, False
        business_settings.clear_cache()
        with self.Session() as db:
            db.add(User(id=1, username="owner", full_name="Jae Owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=3, username="mike", full_name="Mike Tech", password_hash=hash_password(PASSWORD), role="tech"))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100"))
            db.flush()
            db.add(Vehicle(id=1, customer_id=1, year=2005, make="Dodge", model="Ram"))
            db.flush()
            db.add(RepairOrder(id=12, customer_id=1, vehicle_id=1, status="in_progress"))
            db.add_all([
                LineItem(repair_order_id=12, description="Water pump", item_type="labor", quantity=2.5, approval="approved"),
                LineItem(repair_order_id=12, description="Thermostat", item_type="labor", quantity=1.5, approval="pending"),
                LineItem(repair_order_id=12, description="Timing belt", item_type="labor", quantity=3.0, approval="declined"),
                LineItem(repair_order_id=12, description="Pump part", item_type="part", quantity=1.0, unit_price=120),
            ])
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only
        self.env.stop()
        self.clock.stop()
        business_settings.clear_cache()
        self.engine.dispose()

    def client(self, name="owner"):
        client = TestClient(app)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def at(self, when):
        return patch.object(shop_board, "now_local", return_value=when)

    def timer(self, username, when=NOW):
        with self.Session() as db:
            user = db.query(User).filter_by(username=username).one()
            ro = db.get(RepairOrder, 12)
            return job_timer.for_job(db, ro, user, when)

    def test_tech_starts_and_stops_own_clock_from_the_job_page(self):
        tech = self.client("mike")
        with self.at(NOW - timedelta(hours=2)):
            r = tech.post("/ro/12/clock", data={"operation": "start"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("/ro/12", r.headers["location"])
        t = self.timer("mike")
        self.assertTrue(t["mine_on"])
        self.assertEqual(t["mine_seconds"], 7200)
        self.assertEqual((t["clocked"], t["billed"]), (2.0, 4.0))  # declined line and parts don't count
        with self.at(NOW + timedelta(minutes=12)):
            tech.post("/ro/12/clock", data={"operation": "stop"})
        t = self.timer("mike", NOW + timedelta(hours=3))
        self.assertFalse(t["mine_on"])
        self.assertEqual((t["clocked"], t["left"], t["over"]), (2.2, 1.8, False))
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 12).status, "in_progress")  # the job itself is untouched

    def _approved_not_started(self, approval="approved"):
        with self.Session() as db:
            db.add(RepairOrder(id=20, customer_id=1, vehicle_id=1, status="approval_received"))
            db.add(LineItem(repair_order_id=20, description="Brakes", item_type="labor", quantity=2.0, approval=approval))
            db.commit()

    def test_starting_the_clock_on_an_approved_job_moves_it_to_in_progress(self):
        from app.dispatch_model import DispatchAudit
        self._approved_not_started()
        self.client("mike").post("/ro/20/clock", data={"operation": "start"})
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 20).status, "in_progress")
            self.assertEqual(db.query(DispatchAudit).filter_by(action="clock_started_job").count(), 1)
        with self.at(NOW + timedelta(hours=1)):
            self.client("mike").post("/ro/20/clock", data={"operation": "stop"})
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 20).status, "in_progress")  # stopping never moves the job back

    def test_clock_on_a_job_with_every_line_declined_leaves_the_job_alone(self):
        self._approved_not_started(approval="declined")
        self.client().post("/board/clock/20", data={"operation": "start"})
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 20).status, "approval_received")
            self.assertTrue(shop_board.job_clocks(db)[20]["on"])

    def test_tv_shows_booked_hours_until_someone_clocks_in(self):
        self._approved_not_started()  # the newest job on the car is the one the TV shows
        html = self.client().get("/board/panel").text
        self.assertIn("2.0 hr booked", html)
        self.assertNotIn("0.0 of 2.0 hr", html)

    def test_two_workers_add_up_and_stop_on_the_job_page_stops_only_mine(self):
        owner, tech = self.client(), self.client("mike")
        with self.at(NOW - timedelta(hours=3)):
            owner.post("/ro/12/clock", data={"operation": "start"})
        with self.at(NOW - timedelta(hours=2)):
            tech.post("/ro/12/clock", data={"operation": "start"})
        with self.at(NOW):
            owner.post("/ro/12/clock", data={"operation": "stop"})
        t = self.timer("mike", NOW + timedelta(hours=1))
        self.assertTrue(t["mine_on"])  # the owner's stop left the tech's clock running
        self.assertEqual(t["clocked"], 6.0)  # 3 h owner + 3 h tech: person-hours, like book time
        self.assertTrue(t["over"])
        names = {w["name"]: (w["hours"], w["on"]) for w in t["workers"]}
        self.assertEqual(names, {"Mike": (3.0, True), "Jae": (3.0, False)})
        with self.Session() as db:  # the TV shows the same total
            job = {j["id"]: j for j in shop_board.lot_list(db)}[12]
        self.assertTrue(job["clock_on"])
        # The phone page's Stop clock stops everyone on the job
        with self.at(NOW + timedelta(hours=1)):
            self.client().post("/board/clock/12", data={"operation": "stop"})
        t = self.timer("mike", NOW + timedelta(hours=5))
        self.assertFalse(t["anyone_on"])
        self.assertEqual(t["clocked"], 6.0)

    def test_old_single_clock_is_read_as_the_shop_clock(self):
        with self.Session() as db:
            business_settings.save(db, shop_board.CLOCK_KEY, json.dumps({"12": {"on": None, "spent": 5400}}), "test")
            db.commit()
        t = self.timer("owner")
        self.assertEqual(t["clocked"], 1.5)
        self.assertEqual(t["workers"][0]["name"], "Shop clock")
        self.client().post("/ro/12/clock", data={"operation": "start"})
        t = self.timer("owner", NOW + timedelta(minutes=30))
        self.assertEqual(t["clocked"], 2.0)

    def test_hours_clocked_today_split_at_midnight(self):
        night = NOW.replace(hour=22)
        with self.Session() as db:
            user = db.get(User, 3)
            shop_board.set_clock(db, user, 12, True, now=night)
            shop_board.set_clock(db, user, 12, False, now=night + timedelta(hours=3))  # 2 h before midnight, 1 h after
            clocks = shop_board.job_clocks(db)
            self.assertEqual(shop_board.clocked_seconds_on(clocks, night.date(), night + timedelta(hours=4)), 7200)
            self.assertEqual(shop_board.clocked_seconds_on(clocks, night.date() + timedelta(days=1),
                                                           night + timedelta(hours=4)), 3600)
            shop_board.set_clock(db, user, 12, True, now=night + timedelta(hours=10))  # running since 8 AM
            clocks = shop_board.job_clocks(db)
            self.assertEqual(shop_board.clocked_seconds_on(clocks, night.date() + timedelta(days=1),
                                                           night + timedelta(hours=11, minutes=30)), 3600 + 5400)

    def test_picked_up_job_cannot_start_but_keeps_its_hours(self):
        tech = self.client("mike")
        with self.at(NOW - timedelta(hours=1)):
            tech.post("/ro/12/clock", data={"operation": "start"})
        with self.Session() as db:
            db.get(RepairOrder, 12).status = "delivered"
            db.commit()
        tech.post("/ro/12/clock", data={"operation": "stop"})  # a clock left running can still be stopped
        self.assertEqual(tech.post("/ro/12/clock", data={"operation": "start"}).status_code, 404)
        with self.Session() as db:  # another job's clock change doesn't drop this job's hours
            db.add(RepairOrder(id=13, customer_id=1, vehicle_id=1, status="intake"))
            db.commit()
            shop_board.set_clock(db, db.get(User, 3), 13, True, now=NOW)
        t = self.timer("mike")
        self.assertEqual((t["clocked"], t["can_start"]), (1.0, False))
        self.assertEqual(tech.post("/ro/12/clock", data={"operation": "pause"}).status_code, 422)

    def test_job_page_shows_the_clock(self):
        with self.at(NOW - timedelta(minutes=90)):
            self.client("mike").post("/ro/12/clock", data={"operation": "start"})
        html = self.client("mike").get("/ro/12").text
        self.assertIn('id="job-timer"', html)
        self.assertIn("Clocked 1.5 h · Billed 4.0 h", " ".join(html.split()))
        self.assertIn("Stop clock", html)
        self.assertIn("You're clocked in", html)
        owner_html = self.client().get("/ro/12").text
        self.assertIn("Start clock", owner_html)
        self.assertIn("Mike 1.5 h (on)", " ".join(owner_html.split()))


if __name__ == "__main__":
    unittest.main()
