"""Owner money page, on made-up figures in a private database."""
import unittest
from datetime import date, datetime
from unittest.mock import patch

import fixture_environment  # noqa: F401 -- disposable sqlite before app imports
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app import finance_plan as plan
from app.auth import hash_password
from app.database import Base, get_db
from app.finance_model import FinanceAccount, FinanceBill, FinanceChange, FinanceIncoming
from app.main import app
from app.models import User
from app.routes import finance
from app.routes.finance import month_boundaries

PASSWORD = "fixture-password-1"
TODAY = date(2026, 11, 21)  # a Saturday


class PlanMathTests(unittest.TestCase):
    def test_amounts_become_cents(self):
        self.assertEqual(plan.cents("$1,977.39"), 197739)
        self.assertEqual(plan.cents("-214.05"), -21405)
        self.assertEqual(plan.cents("75"), 7500)
        self.assertEqual(plan.cents("0.5"), 50)
        with self.assertRaises(ValueError):
            plan.cents("lots")

    def test_short_dates_pick_the_right_year(self):
        self.assertEqual(plan.parse_date("12/4", TODAY), date(2026, 12, 4))
        self.assertEqual(plan.parse_date("1/3", TODAY), date(2027, 1, 3))
        self.assertEqual(plan.parse_date("11/17", TODAY), date(2026, 11, 17))
        self.assertEqual(plan.parse_date("2026-12-09", TODAY), date(2026, 12, 9))

    def test_monthly_bill_keeps_its_day_or_the_last_day(self):
        self.assertEqual(plan.next_due(date(2026, 1, 31), "monthly"), date(2026, 2, 28))
        self.assertEqual(plan.next_due(date(2026, 12, 9), "monthly"), date(2027, 1, 9))
        self.assertIsNone(plan.next_due(date(2026, 12, 9), "once"))

    def test_sundays_are_not_open_days(self):
        self.assertEqual(plan.working_days(date(2026, 11, 21), date(2026, 11, 28)), 7)

    def test_paste_reads_each_kind_and_reports_bad_lines(self):
        lines, problems = plan.parse_paste(
            "balance | Shop checking | 174.80 | business\n"
            "balance | Apple Cash | 413.55 | held 413.55\n"
            "bill | Power | 514.85 | 12/4 | utilities | monthly\n"
            "owed | Pat Sample | 1,977.39 | 11/30\n"
            "paid | Power\n"
            "bill | Rent | 1879\n"
            "# a note\n"
            "nonsense line\n", TODAY)
        self.assertEqual([l.kind for l in lines], ["balance", "balance", "bill", "owed", "paid"])
        self.assertEqual(lines[1].held, 41355)
        self.assertEqual((lines[2].category, lines[2].repeats, lines[2].when), ("utilities", "monthly", date(2026, 12, 4)))
        self.assertEqual(len(problems), 2)
        self.assertIn("Line 6", problems[0])

    def test_business_and_personal_never_mix(self):
        accounts = [FinanceAccount(name="Shop", scope="business", balance_cents=17480, held_cents=0, as_of=TODAY, active=True),
                    FinanceAccount(name="Wallet", scope="business", balance_cents=41355, held_cents=41355, as_of=TODAY, active=True),
                    FinanceAccount(name="Mine", scope="personal", balance_cents=-13154, held_cents=0, as_of=TODAY, active=True)]
        bills = [FinanceBill(name="Rent", scope="business", amount_cents=187900, due_date=date(2026, 11, 17), status="open", repeats="monthly", category="rent"),
                 FinanceBill(name="Power", scope="business", amount_cents=51485, due_date=date(2026, 12, 4), status="open", repeats="once", category="utilities"),
                 FinanceBill(name="Later", scope="business", amount_cents=1000, due_date=date(2027, 1, 4), status="open", repeats="once", category="other"),
                 FinanceBill(name="Phone", scope="personal", amount_cents=7110, due_date=date(2026, 11, 25), status="open", repeats="once", category="phone")]
        incoming = [FinanceIncoming(source="Pat", scope="business", amount_cents=30200, expected_date=date(2026, 11, 24), status="expected"),
                    FinanceIncoming(source="No date", scope="business", amount_cents=99900, expected_date=None, status="expected")]
        sums = plan.totals(accounts, bills, incoming, TODAY, 14)
        shop = sums["business"]
        self.assertEqual(shop.cash, 17480)
        self.assertEqual(shop.held, 41355)
        self.assertEqual(shop.overdue, 187900)
        self.assertEqual(shop.due_in_window, 51485)
        self.assertEqual(shop.expected_in, 30200)
        self.assertEqual(shop.short, 187900 + 51485 - 17480 - 30200)
        self.assertEqual([b.name for b in shop.later_bills], ["Later"])
        self.assertEqual(sums["personal"].owed, 7110)
        self.assertEqual(sums["days_open"], 12)
        self.assertEqual(plan.per_day(shop.short, sums["days_open"]), -(-shop.short // 12))


class FinancePageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.store_patch = patch.object(store, "SessionLocal", self.Session)
        self.store_patch.start()
        self.today_patch = patch.object(finance, "phoenix_today", return_value=TODAY)
        self.today_patch.start()
        store.clear_cache()
        self.read_only = app.state.read_only
        app.state.read_only = True  # production is read-only, and money figures must still save
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            db.commit()

    def tearDown(self):
        self.store_patch.stop()
        self.today_patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def test_signed_out_goes_to_login_and_staff_are_kept_out(self):
        self.assertEqual(TestClient(app, follow_redirects=False).get("/finance/").headers["location"], "/login")
        tech = self.sign_in("tech")
        self.assertEqual(tech.get("/finance/").status_code, 403)
        self.assertEqual(tech.post("/finance/paste", data={"figures": "bill | X | 5 | 12/1"}).status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.query(FinanceBill).count(), 0)

    def test_empty_page_shows_and_saves_nothing(self):
        owner = self.sign_in()
        page = owner.get("/finance/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Cash you can use", page.text)
        self.assertIn("No balances yet", page.text)
        with self.Session() as db:
            self.assertEqual(db.query(FinanceChange).count(), 0)

    def test_pasted_figures_drive_the_overview(self):
        owner = self.sign_in()
        saved = owner.post("/finance/paste", data={"window": "14", "figures": (
            "balance | Shop checking | 174.80\n"
            "balance | Apple Cash | 413.55 | held 413.55\n"
            "balance | My checking | -131.54 | personal\n"
            "bill | Shop rent | 1,879 | 11/17 | rent | monthly\n"
            "bill | Power | 514.85 | 12/4 | utilities\n"
            "bill | Phone | 71.10 | 11/25 | personal | phone\n"
            "owed | Pat Sample | 302 | 11/24\n")})
        self.assertEqual(saved.status_code, 303)
        self.assertIn("Saved+7+figures", saved.headers["location"])
        page = owner.get("/finance/?window=14").text
        self.assertIn("$174.80", page)                  # cash you can use, deposit held back
        self.assertIn("$2,393.85", page)                # rent past due + power
        self.assertIn("$1,879.00 of it is past due", page)
        self.assertIn("$302.00", page)
        self.assertIn("Short by", page)
        self.assertIn("$1,917.05", page)                # 2,393.85 - 174.80 - 302.00
        self.assertIn("$159.76 a day", page)            # over 12 open days
        self.assertIn("4 days late", page)
        self.assertIn("Cash -$131.54 · bills due $71.10", page)

    def test_paying_a_monthly_bill_adds_next_month(self):
        owner = self.sign_in()
        owner.post("/finance/paste", data={"figures": "bill | Shop rent | 1879 | 11/17 | rent | monthly"})
        with self.Session() as db:
            bill_id = db.query(FinanceBill).one().id
        done = owner.post(f"/finance/bills/{bill_id}/paid", data={"window": "30"})
        self.assertIn("window=30", done.headers["location"])
        with self.Session() as db:
            rows = db.query(FinanceBill).order_by(FinanceBill.due_date).all()
            self.assertEqual([(r.status, r.due_date) for r in rows], [("paid", date(2026, 11, 17)), ("open", date(2026, 12, 17))])
            self.assertEqual(rows[0].paid_on, TODAY)
            self.assertTrue(db.query(FinanceChange).filter(FinanceChange.action == "bill_paid").count())
        again = owner.post(f"/finance/bills/{bill_id}/paid", data={})
        self.assertIn("already+closed", again.headers["location"])

    def test_same_name_updates_and_got_it_counts_as_money_in(self):
        owner = self.sign_in()
        owner.post("/finance/paste", data={"figures": "owed | Pat Sample | 300 | 11/24\nbalance | Shop checking | 10"})
        owner.post("/finance/paste", data={"figures": "owed | pat sample | 302 | 11/25\nbalance | SHOP CHECKING | 20"})
        with self.Session() as db:
            self.assertEqual(db.query(FinanceIncoming).one().amount_cents, 30200)
            self.assertEqual(db.query(FinanceAccount).one().balance_cents, 2000)
            item_id = db.query(FinanceIncoming).one().id
        owner.post(f"/finance/incoming/{item_id}/got", data={})
        page = owner.get("/finance/").text
        self.assertIn("Nothing expected from Nobody", owner.post("/finance/paste", data={"figures": "got | Nobody"}).headers["location"].replace("+", " "))
        self.assertIn("$302.00", page)  # today's money in

    def test_add_form_and_goal(self):
        owner = self.sign_in()
        added = owner.post("/finance/add", data={"kind": "bill", "name": "Software", "amount": "284.44", "when": "2026-11-23",
                                                 "scope": "business", "category": "software", "repeats": "monthly"})
        self.assertIn("Saved", added.headers["location"])
        missing = owner.post("/finance/add", data={"kind": "bill", "name": "No date", "amount": "5", "scope": "business"})
        self.assertIn("needs+a+due+date", missing.headers["location"])
        self.assertEqual(owner.post("/finance/goal", data={"goal": "650"}).status_code, 303)
        page = owner.get("/finance/").text
        self.assertIn("Daily goal: $650.00", page)
        self.assertIn("Software", page)

    def test_missing_tables_show_a_notice_instead_of_failing(self):
        for name in ("finance_changes", "finance_incoming", "finance_bills", "finance_accounts"):
            Base.metadata.tables[name].drop(self.engine)
        owner = self.sign_in()
        page = owner.get("/finance/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("isn't set up on this server yet", page.text)
        saved = owner.post("/finance/paste", data={"figures": "bill | X | 5 | 12/1"})
        self.assertIn("nothing+was+saved", saved.headers["location"])

    def test_month_bounds_are_exclusive_and_phoenix_local(self):
        # Arizona is UTC-7 year-round. November and December start at 07:00 UTC.
        self.assertEqual(month_boundaries("2026-11"), (datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 7)))
        self.assertEqual(month_boundaries("2026-12"), (datetime(2026, 12, 1, 7), datetime(2027, 1, 1, 7)))


if __name__ == "__main__":
    unittest.main()
