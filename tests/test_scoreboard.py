"""Home's daily scoreboard: money in today against the daily goal, the week so far, hours clocked today."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings, scoreboard, shop_board
from app.appointments import PHOENIX
from app.auth import hash_password
from app.database import Base, get_db
from app.finance_model import FinanceIncoming
from app.main import app
from app.models import Customer, Invoice, JobDeposit, Payment, RepairOrder, User, Vehicle
from app.routes import finance
from app.routes.shop_board import templates

PASSWORD = "fixture-password-1"
NOW = datetime(2026, 11, 25, 15, 0, tzinfo=PHOENIX)  # a Wednesday: Mon-Wed are three open days


def utc(local):
    return local.astimezone(timezone.utc).replace(tzinfo=None)


class ScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.env = patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "false"})
        self.env.start()
        self.clock = patch.object(shop_board, "now_local", return_value=NOW)
        self.clock.start()
        self.store = patch.object(business_settings, "SessionLocal", self.Session)  # settings read from this database
        self.store.start()
        self.read_only, app.state.read_only = app.state.read_only, False
        business_settings.clear_cache()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=3, username="mike", password_hash=hash_password(PASSWORD), role="tech"))
            db.add(Customer(id=1, first_name="Pat", last_name="Sample"))
            db.flush()
            db.add(Vehicle(id=1, customer_id=1, year=2016, make="Ford", model="Focus"))
            db.flush()
            db.add_all([RepairOrder(id=5, customer_id=1, vehicle_id=1, status="delivered"),
                        RepairOrder(id=6, customer_id=1, vehicle_id=1, status="in_progress")])
            db.flush()
            db.add(Invoice(id=1, repair_order_id=5, total=9000, status="sent"))
            db.flush()
            moved = Payment(invoice_id=1, amount=50, paid_at=utc(NOW - timedelta(hours=5)))  # a deposit moved onto the invoice
            db.add_all([
                Payment(invoice_id=1, amount=200, paid_at=utc(NOW - timedelta(hours=2))),
                Payment(invoice_id=1, amount=125.5, paid_at=utc(NOW.replace(hour=0, minute=30))),
                moved,
                # 11 PM yesterday in Phoenix is already today in UTC: yesterday, not today
                Payment(invoice_id=1, amount=999, paid_at=utc(NOW.replace(hour=0) - timedelta(hours=1))),
                Payment(invoice_id=1, amount=300, paid_at=utc(NOW - timedelta(days=2))),   # Monday: this week
                Payment(invoice_id=1, amount=5000, paid_at=utc(NOW - timedelta(days=3))),  # Sunday: last week
            ])
            db.flush()
            db.add_all([
                JobDeposit(repair_order_id=6, amount=100, method="zelle", received_at=utc(NOW - timedelta(hours=1))),
                JobDeposit(repair_order_id=5, amount=50, method="cash", received_at=utc(NOW - timedelta(hours=5)),
                           payment_id=moved.id),  # already the 50 payment above: counted once
                JobDeposit(repair_order_id=6, amount=75, method="cash", received_at=utc(NOW - timedelta(hours=1)),
                           removed_at=datetime.now(timezone.utc)),
                FinanceIncoming(source="Got it", amount_cents=7000, status="received", scope="business", received_on=NOW.date()),
                FinanceIncoming(source="Personal", amount_cents=4000, status="received", scope="personal", received_on=NOW.date()),
                FinanceIncoming(source="Expected", amount_cents=9000, status="expected", scope="business",
                                expected_date=NOW.date()),
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
        self.store.stop()
        business_settings.clear_cache()
        self.engine.dispose()

    def board(self):
        business_settings.clear_cache()
        with self.Session() as db:
            return scoreboard.scoreboard(db, NOW)

    def test_today_counts_payments_deposits_and_got_it_but_not_yesterday(self):
        s = self.board()
        self.assertEqual(s["today"], 545.5)  # 200 + 125.50 + 50 moved deposit + 100 open deposit + 70 "Got it"
        self.assertEqual((s["goal"], s["source"]), (500, "fixed"))
        self.assertTrue(s["hit"])
        self.assertEqual(s["percent"], 100)
        self.assertEqual(s["week"], 545.5 + 999 + 300)  # Sunday's 5000 is last week
        self.assertEqual((s["days_so_far"], s["week_goal"]), (3, 1500))
        self.assertEqual(s["week_percent"], 100)

    def test_short_of_goal_shows_what_is_left(self):
        with self.Session() as db:
            db.query(FinanceIncoming).delete()
            db.query(JobDeposit).delete()
            db.commit()
        s = self.board()
        self.assertEqual((s["today"], s["left"], s["hit"], s["percent"]), (375.5, 124.5, False, 75))
        html = templates.env.get_template("_scoreboard.html").render(scoreboard=s)
        flat = " ".join(html.split())
        self.assertIn("$376 <small>of $500 today</small>", flat)
        self.assertIn("$124 to go", flat)
        self.assertIn("Goal: your fixed $500 a day", flat)

    def test_owner_sets_fixed_goal_and_picks_bills(self):
        owner = TestClient(app)
        owner.post("/login", data={"username": "owner", "password": PASSWORD})
        self.assertEqual(owner.post("/board/goal", data={"goal": "$650"}, follow_redirects=False).status_code, 303)
        s = self.board()
        self.assertEqual((s["goal"], s["source"], s["hit"]), (650, "fixed", False))
        owner.post("/board/goal", data={"goal": "650", "mode": "bills"})
        with patch.object(finance, "phoenix_today", return_value=NOW.date()), \
                patch.object(finance, "home_goal", return_value={"need": 812.0}):
            s = self.board()
        self.assertEqual((s["goal"], s["source"], s["fixed_goal"]), (812.0, "bills", 650))
        html = templates.env.get_template("_scoreboard.html").render(scoreboard=s)
        self.assertIn("what the bills need a day", html)
        with patch.object(finance, "phoenix_today", return_value=NOW.date()), \
                patch.object(finance, "home_goal", return_value={"need": 0}):
            self.assertEqual(self.board()["goal"], 650)  # bills covered: back to the fixed goal
        self.assertEqual(owner.post("/board/goal", data={"goal": "650", "mode": "whatever"},
                                    follow_redirects=False).headers["location"].count("error="), 1)
        tech = TestClient(app)
        tech.post("/login", data={"username": "mike", "password": PASSWORD})
        self.assertEqual(tech.post("/board/goal", data={"goal": "1"}).status_code, 403)

    def test_home_goal_bar_follows_the_mode(self):
        with self.Session() as db:
            bar = finance.home_goal(db)
        self.assertEqual((bar["target"], bar["source"]), (500, "fixed"))

    def test_hours_clocked_today(self):
        with self.Session() as db:
            shop_board.set_clock(db, db.get(User, 3), 6, True, now=NOW - timedelta(hours=2))
            shop_board.set_clock(db, db.get(User, 1), 6, True, now=NOW - timedelta(minutes=30))
        self.assertEqual(self.board()["hours_today"], 2.5)
        html = templates.env.get_template("_scoreboard.html").render(scoreboard=self.board())
        self.assertIn("2.5 h clocked today", html)


if __name__ == "__main__":
    unittest.main()
