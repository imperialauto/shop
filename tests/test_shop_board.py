"""Shop TV board on made-up records: jobs, money in today, and the phone-run to-do list."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings, shop_board, task_board
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.auth import hash_password
from app.database import Base, get_db
from app.imperial_task_model import BOARD_CATEGORY, ImperialTask
from app.main import app
from app.models import Customer, Invoice, Payment, RepairOrder, User, Vehicle

PASSWORD = "fixture-password-1"
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=PHOENIX)


class ShopBoardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.env = patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"})
        self.env.start()
        self.clock = patch.object(shop_board, "now_local", return_value=NOW)
        self.clock.start()
        self.read_only = app.state.read_only
        app.state.read_only = False
        business_settings.clear_cache()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.add(User(id=3, username="mike", full_name="Mike Sample", password_hash=hash_password(PASSWORD), role="tech"))
            pat = Customer(first_name="Pat", last_name="Sample", phone="+16025550100")
            lee = Customer(first_name="Unknown (0101)", last_name="-", phone="+16025550101")
            db.add_all([pat, lee])
            db.flush()
            focus = Vehicle(customer_id=pat.id, year=2016, make="Ford", model="Focus")
            ram = Vehicle(customer_id=lee.id, year=2019, make="Ram", model="5500")
            db.add_all([focus, ram])
            db.flush()
            self.visit_job = RepairOrder(customer_id=pat.id, vehicle_id=focus.id, status="intake")
            self.shop_job = RepairOrder(customer_id=lee.id, vehicle_id=ram.id, status="in_progress", assigned_tech_id=3)
            gone = RepairOrder(customer_id=pat.id, vehicle_id=focus.id, status="delivered")
            db.add_all([self.visit_job, self.shop_job, gone])
            db.flush()
            db.add(Appointment(repair_order_id=self.visit_job.id, resource_id="bay-1", status="confirmed",
                               starts_at=datetime(2026, 11, 23, 14, 30, tzinfo=PHOENIX).astimezone(timezone.utc),
                               ends_at=datetime(2026, 11, 23, 15, 30, tzinfo=PHOENIX).astimezone(timezone.utc)))
            invoice = Invoice(repair_order_id=gone.id, total=900, status="sent")
            db.add(invoice)
            db.flush()
            utc = lambda local: local.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
            db.add(Payment(invoice_id=invoice.id, amount=200, paid_at=utc(NOW - timedelta(hours=2))))
            db.add(Payment(invoice_id=invoice.id, amount=125.5, paid_at=utc(NOW.replace(hour=0, minute=30))))
            # 11 PM yesterday in Phoenix is already today in UTC: it must not count.
            db.add(Payment(invoice_id=invoice.id, amount=999, paid_at=utc(NOW.replace(hour=0) - timedelta(hours=1))))
            db.commit()
            self.visit_id, self.shop_id = self.visit_job.id, self.shop_job.id

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

    def test_jobs_put_todays_visits_first_and_show_no_full_names(self):
        with self.Session() as db:
            jobs = shop_board.jobs_today(db, NOW)
        self.assertEqual([j["id"] for j in jobs], [self.visit_id, self.shop_id])
        self.assertEqual((jobs[0]["time"], jobs[0]["who"], jobs[0]["vehicle"]), ("2:30 PM", "Pat S.", "2016 Ford Focus"))
        self.assertEqual((jobs[1]["time"], jobs[1]["who"], jobs[1]["stage"], jobs[1]["tech"]),
                         (None, "", "Working on it", "Mike"))

    def test_money_in_today_uses_the_phoenix_day_and_the_goal(self):
        with self.Session() as db:
            sales = shop_board.sales_today(db, NOW)
        self.assertEqual((sales["total"], sales["goal"], sales["count"]), (325.5, 500, 2))
        self.assertEqual((sales["percent"], sales["left"], sales["hit"]), (65, 174.5, False))

    def test_tv_page_and_refresh_panel(self):
        client = self.client()
        page = client.get("/board")
        self.assertEqual(page.status_code, 200)
        for text in ("Today's jobs", "Money in today", "$326", "of $500", "2016 Ford Focus", 'hx-get="/board/panel"'):
            self.assertIn(text, page.text)
        self.assertNotIn("+16025550100", page.text)
        self.assertNotIn("Sample", page.text.replace("Pat S.", ""))
        panel = client.get("/board/panel")
        self.assertEqual(panel.status_code, 200)
        self.assertNotIn("<html", panel.text)
        self.assertIn("Working on it", panel.text)

    def test_money_only_shows_to_the_owner(self):
        page = self.client("office").get("/board")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Money in today", page.text)
        self.assertIn("Today's jobs", page.text)

    def test_signed_out_screen_says_so_instead_of_going_blank(self):
        panel = TestClient(app).get("/board/panel")
        self.assertEqual(panel.status_code, 200)
        self.assertIn("This screen was signed out", panel.text)
        tv = TestClient(app).get("/board", headers={"accept": "text/html"}, follow_redirects=False)
        self.assertEqual(tv.status_code, 303)

    def test_phone_adds_and_ticks_off_a_todo_and_the_tv_follows(self):
        phone, tv = self.client(), self.client()
        saved = phone.post("/board/todos", data={"title": "  Sweep bay 2 ", "urgent": "1"}, follow_redirects=False)
        self.assertEqual(saved.status_code, 303)
        phone.post("/board/todos", data={"title": "Order oil filters"})
        panel = tv.get("/board/panel").text
        self.assertLess(panel.index("Sweep bay 2"), panel.index("Order oil filters"))
        self.assertIn("todo urgent", panel)
        with self.Session() as db:
            task = db.query(ImperialTask).filter_by(title="Sweep bay 2").one()
            self.assertEqual((task.category, task.priority), (BOARD_CATEGORY, "urgent"))
        phone.post(f"/board/todos/{task.id}", data={"operation": "done"})
        panel = tv.get("/board/panel").text
        self.assertIn('class="todo done">Sweep bay 2', panel)
        self.assertIn("1 left", panel)
        phone.post(f"/board/todos/{task.id}", data={"operation": "undo"})
        self.assertIn("2 left", tv.get("/board/panel").text)
        manage = phone.get("/board/manage")
        self.assertEqual(manage.status_code, 200)
        self.assertIn("Sweep bay 2", manage.text)
        self.assertIn("Daily sales goal", manage.text)

    def test_techs_see_the_board_and_tick_todos_but_not_money(self):
        tech = self.client("mike")
        page = tech.get("/board")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Money in today", page.text)
        tech.post("/board/todos", data={"title": "Clean the lift"})
        with self.Session() as db:
            task_id = db.query(ImperialTask).filter_by(title="Clean the lift").one().id
        self.assertEqual(tech.post(f"/board/todos/{task_id}", data={"operation": "done"}, follow_redirects=False).status_code, 303)
        self.assertEqual(tech.post("/board/goal", data={"goal": "1"}).status_code, 403)

    def test_short_or_blank_todo_is_refused_with_a_message(self):
        response = self.client().post("/board/todos", data={"title": " "})
        self.assertIn("between 2 and 200", response.text)

    def test_board_cant_tick_other_kinds_of_task(self):
        with self.Session() as db:
            other = ImperialTask(title="Call Pat back", source="dashboard", external_id="x1", category="owner_todo")
            db.add(other)
            db.commit()
            other_id = other.id
        response = self.client().post(f"/board/todos/{other_id}", data={"operation": "done"})
        self.assertEqual(response.status_code, 404)
        with self.Session() as db:
            self.assertEqual(db.get(ImperialTask, other_id).status, "open")

    def test_board_todos_stay_off_the_task_tracker(self):
        with self.Session() as db:
            db.add(ImperialTask(title="Sweep bay 2", source="shop_board", external_id="b1", category=BOARD_CATEGORY))
            db.commit()
            data = task_board.board(db, linked=None)
        titles = [card["headline"] for cards in data["cards"].values() for card in cards]
        self.assertNotIn("Sweep bay 2", titles)

    def test_owner_sets_the_goal_and_office_cannot(self):
        self.assertEqual(self.client("office").post("/board/goal", data={"goal": "700"}).status_code, 403)
        owner = self.client()
        owner.post("/board/goal", data={"goal": "$750"})
        business_settings.clear_cache()
        with patch.object(business_settings, "_load", return_value={"daily_sales_goal": "750"}):
            self.assertEqual(shop_board.daily_goal(), 750)
        bad = owner.post("/board/goal", data={"goal": "lots"})
        self.assertIn("dollar amount", bad.text)


if __name__ == "__main__":
    unittest.main()
