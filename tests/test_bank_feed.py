"""Bank feed on the Money page, against a pretend Plaid. No network, no real bank data."""
import asyncio
import json
import os
import unittest
from datetime import date
from unittest.mock import patch

import fixture_environment  # noqa: F401 -- disposable sqlite before app imports
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import bank_feed as feed
from app import business_settings as store
from app import scheduler
from app.auth import hash_password
from app.bank_model import BankAccount, BankLink, BankTransaction
from app.database import Base, get_db
from app.finance_model import FinanceAccount, FinanceBill, FinanceChange
from app.main import app
from app.models import User
from app.routes import finance

PASSWORD = "fixture-password-1"
TODAY = date(2026, 11, 23)
KEYS = {"PLAID_CLIENT_ID": "fixture-client", "PLAID_SECRET": "fixture-secret", "PLAID_ENV": "sandbox"}


def plaid_account(account_id, name, kind="depository", subtype="checking", mask="1234", available=None, current=None):
    return {"account_id": account_id, "name": name, "mask": mask, "type": kind, "subtype": subtype,
            "balances": {"available": available, "current": current, "iso_currency_code": "USD"}}


def plaid_txn(tid, account_id, amount, day, name, pending=False):
    """Plaid's sign: money out is positive."""
    return {"transaction_id": tid, "account_id": account_id, "amount": amount, "date": day, "name": name,
            "merchant_name": None, "pending": pending, "personal_finance_category": {"primary": "GENERAL_SERVICES"}}


class FakePlaid:
    """Stands in for PlaidClient. pages is a list of /transactions/sync answers, served in order."""

    def __init__(self, accounts=None, pages=None, errors=None):
        self.accounts_rows = accounts or []
        self.pages = list(pages or [])
        self.errors = dict(errors or {})
        self.removed, self.cursors, self.exchanged = [], [], []

    def _maybe_fail(self, name):
        if name in self.errors:
            raise feed.PlaidError(self.errors.pop(name))

    def link_token(self, user_id, access_token=None):
        self._maybe_fail("link_token")
        return "link-sandbox-fixture"

    def exchange(self, public_token):
        self._maybe_fail("exchange")
        self.exchanged.append(public_token)
        return "access-sandbox-fixture-token", "item-fixture-1"

    def accounts(self, access_token):
        self._maybe_fail("accounts")
        return self.accounts_rows

    def transactions_sync(self, access_token, cursor):
        self._maybe_fail("sync")
        self.cursors.append(cursor)
        return self.pages.pop(0) if self.pages else {"added": [], "modified": [], "removed": [], "next_cursor": cursor, "has_more": False}

    def remove(self, access_token):
        self.removed.append(access_token)


CHASE_ACCOUNTS = [
    plaid_account("acc-chk", "Performance Business Checking", available=1200.50, current=1300.00),
    plaid_account("acc-card", "Ink Business Card", kind="credit", subtype="credit card", mask="9876", current=450.25),
]


class DbCase(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def link(self, db, institution="Chase"):
        link = BankLink(item_id=f"item-{institution}", institution=institution, access_token_enc=feed.seal("access-sandbox-x"))
        db.add(link)
        db.flush()
        return link


class SealTests(unittest.TestCase):
    def test_bank_token_is_stored_encrypted_and_reads_back(self):
        sealed = feed.seal("access-production-abc")
        self.assertNotIn("access-production-abc", sealed)
        self.assertEqual(feed.unseal(sealed), "access-production-abc")
        self.assertIsNone(feed.unseal("not a sealed token"))

    def test_keys_switch_the_feed_on(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PLAID_CLIENT_ID", None)
            os.environ.pop("PLAID_SECRET", None)
            self.assertFalse(feed.enabled())
        with patch.dict(os.environ, KEYS):
            self.assertTrue(feed.enabled())
            self.assertEqual(feed.settings()["env"], "sandbox")
        with patch.dict(os.environ, {**KEYS, "PLAID_ENV": "nonsense"}):
            self.assertEqual(feed.settings()["env"], "production")


class PlaidClientTests(unittest.TestCase):
    def client(self, handler, **env):
        with patch.dict(os.environ, {**KEYS, **env}):
            return feed.PlaidClient(http=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_new_bank_asks_for_transactions_and_a_reconnect_does_not(self):
        seen = []

        def handler(request):
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"link_token": "link-sandbox-1"})

        plaid = self.client(handler, PLAID_REDIRECT_URI="https://example.test/finance/bank/oauth")
        self.assertEqual(plaid.link_token(1), "link-sandbox-1")
        plaid.link_token(1, access_token="access-sandbox-x")
        (path, first), (_, second) = seen
        self.assertEqual(path, "/link/token/create")
        self.assertEqual(first["products"], ["transactions"])
        self.assertEqual(first["redirect_uri"], "https://example.test/finance/bank/oauth")
        self.assertEqual(first["client_id"], "fixture-client")
        self.assertNotIn("products", second)
        self.assertEqual(second["access_token"], "access-sandbox-x")
        self.assertTrue(plaid.base.startswith("https://sandbox.plaid.com"))

    def test_plaid_errors_carry_the_code_and_never_the_keys(self):
        plaid = self.client(lambda request: httpx.Response(400, json={
            "error_type": "ITEM_ERROR", "error_code": "ITEM_LOGIN_REQUIRED", "error_message": "secret fixture-secret",
            "display_message": None}))
        with self.assertRaises(feed.PlaidError) as caught:
            plaid.accounts("access-sandbox-x")
        self.assertEqual(caught.exception.code, "ITEM_LOGIN_REQUIRED")
        self.assertNotIn("fixture-secret", str(caught.exception))
        self.assertEqual(feed.short_error(caught.exception)[0], "needs_login")

    def test_network_trouble_is_a_plain_error(self):
        def handler(request):
            raise httpx.ConnectError("down")
        with self.assertRaises(feed.PlaidError) as caught:
            self.client(handler).accounts("access-sandbox-x")
        self.assertEqual(caught.exception.code, "NETWORK")


class PullTests(DbCase):
    def test_first_pull_sets_balances_saves_activity_and_marks_the_matching_bill_paid(self):
        page = {"added": [plaid_txn("t1", "acc-chk", 514.85, "2026-11-21", "APS"),
                          plaid_txn("t2", "acc-chk", -302.00, "2026-11-22", "Zelle from Pat Sample"),
                          plaid_txn("t3", "acc-chk", 89.99, "2026-11-22", "AutoZone", pending=True),
                          plaid_txn("t4", "acc-card", 42.10, "2026-11-20", "Fuel")],
                "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}
        plaid = FakePlaid(CHASE_ACCOUNTS, [page])
        with self.Session() as db:
            db.add(FinanceBill(name="APS electric", scope="business", category="utilities", amount_cents=51485,
                               due_date=date(2026, 11, 25), repeats="monthly", status="open"))
            db.add(FinanceBill(name="Phone", scope="personal", category="phone", amount_cents=8999,
                               due_date=date(2026, 11, 24), repeats="once", status="open"))
            self.link(db)
            db.commit()
            result = feed.pull_all(db, plaid, TODAY)
            self.assertEqual((result.banks, result.new_transactions, result.problems), (1, 4, []))
            self.assertEqual(len(result.bills_paid), 1)

            checking = db.query(BankAccount).filter_by(plaid_account_id="acc-chk").one()
            cash = db.get(FinanceAccount, checking.finance_account_id)
            self.assertEqual(cash.name, "Chase Performance Business Checking ...1234")
            self.assertEqual((cash.balance_cents, cash.scope, cash.as_of), (120050, "business", TODAY))
            card = db.query(BankAccount).filter_by(plaid_account_id="acc-card").one()
            self.assertIsNone(card.finance_account_id)  # a card balance is owed, never counted as cash
            self.assertEqual(card.current_cents, 45025)

            amounts = {t.plaid_transaction_id: t.amount_cents for t in db.query(BankTransaction)}
            self.assertEqual(amounts, {"t1": -51485, "t2": 30200, "t3": -8999, "t4": -4210})
            aps = db.query(FinanceBill).filter_by(name="APS electric").order_by(FinanceBill.due_date).all()
            self.assertEqual([(b.status, b.due_date) for b in aps], [("paid", date(2026, 11, 25)), ("open", date(2026, 12, 25))])
            self.assertEqual(aps[0].paid_on, date(2026, 11, 21))
            # the pending $89.99 out of the shop account never pays the personal phone bill
            self.assertEqual(db.query(FinanceBill).filter_by(name="Phone").one().status, "open")
            link = db.query(BankLink).one()
            self.assertEqual((link.cursor, link.status), ("c1", "ok"))
            self.assertIsNotNone(link.last_synced_at)
            self.assertTrue(db.query(FinanceChange).filter_by(actor="bank feed", action="bill_paid").count())

    def test_later_pull_applies_changes_and_never_pays_twice(self):
        first = {"added": [plaid_txn("t1", "acc-chk", 514.85, "2026-11-21", "APS")], "modified": [], "removed": [],
                 "next_cursor": "c1", "has_more": False}
        second = {"added": [], "modified": [plaid_txn("t1", "acc-chk", 514.85, "2026-11-21", "APS ELECTRIC")],
                  "removed": [], "next_cursor": "c2", "has_more": True}
        third = {"added": [], "modified": [], "removed": [{"transaction_id": "t1"}], "next_cursor": "c3", "has_more": False}
        plaid = FakePlaid(CHASE_ACCOUNTS, [first, second, third])
        with self.Session() as db:
            db.add(FinanceBill(name="APS electric", scope="business", category="utilities", amount_cents=51485,
                               due_date=date(2026, 11, 25), repeats="monthly", status="open"))
            self.link(db)
            db.commit()
            feed.pull_all(db, plaid, TODAY)
            result = feed.pull_all(db, plaid, TODAY)
            self.assertEqual(plaid.cursors, [None, "c1", "c2"])
            self.assertEqual(result.bills_paid, [])
            self.assertEqual(db.query(BankTransaction).count(), 0)
            self.assertEqual(db.query(BankLink).one().cursor, "c3")
            self.assertEqual(db.query(FinanceBill).filter_by(status="open").count(), 1)  # just next month's

    def test_one_bank_wanting_a_new_sign_in_does_not_stop_the_others(self):
        with self.Session() as db:
            self.link(db, "Chase")
            db.commit()
            plaid = FakePlaid(CHASE_ACCOUNTS, errors={"accounts": "ITEM_LOGIN_REQUIRED"})
            result = feed.pull_all(db, plaid, TODAY)
            self.assertEqual(result.banks, 0)
            self.assertIn("sign in again", result.problems[0])
            link = db.query(BankLink).one()
            self.assertEqual(link.status, "needs_login")
            # the next good pull clears it
            feed.pull_all(db, FakePlaid(CHASE_ACCOUNTS), TODAY)
            db.refresh(link)
            self.assertEqual((link.status, link.last_error), ("ok", None))

    def test_a_change_mid_pull_starts_the_pull_over(self):
        plaid = FakePlaid(CHASE_ACCOUNTS, [{"added": [plaid_txn("t1", "acc-chk", 5, "2026-11-22", "A")], "modified": [],
                                            "removed": [], "next_cursor": "c1", "has_more": True}])
        real_sync = plaid.transactions_sync
        calls = {"n": 0}

        def flaky(token, cursor):
            calls["n"] += 1
            if calls["n"] == 2:
                raise feed.PlaidError("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION")
            return real_sync(token, cursor)

        plaid.transactions_sync = flaky
        with self.Session() as db:
            self.link(db)
            db.commit()
            result = feed.pull_all(db, plaid, TODAY)
            self.assertEqual(result.problems, [])
            self.assertEqual(plaid.cursors[-1], None)  # started over from the saved cursor
            self.assertEqual(db.query(BankTransaction).count(), 1)

    def test_an_account_left_out_keeps_its_balance_out_of_the_numbers(self):
        with self.Session() as db:
            link = self.link(db)
            feed.save_accounts(db, link, CHASE_ACCOUNTS, TODAY)
            checking = db.query(BankAccount).filter_by(plaid_account_id="acc-chk").one()
            checking.tracked = False
            cash = db.get(FinanceAccount, checking.finance_account_id)
            cash.active = False
            db.commit()
            feed.save_accounts(db, link, [plaid_account("acc-chk", "Performance Business Checking", available=5.00, current=5.00)], TODAY)
            db.refresh(cash)
            self.assertEqual((cash.balance_cents, cash.active), (120050, False))

    def test_disconnect_tells_plaid_and_keeps_the_money_page_account(self):
        plaid = FakePlaid(CHASE_ACCOUNTS)
        with self.Session() as db:
            link = self.link(db)
            feed.pull_link(db, plaid, link, TODAY)
            db.add(BankTransaction(plaid_transaction_id="t9", bank_account_id=db.query(BankAccount).first().id,
                                   day=TODAY, name="X", amount_cents=-100))
            db.commit()
            feed.disconnect(db, plaid, link)
            db.commit()
            self.assertEqual(plaid.removed, ["access-sandbox-x"])
            self.assertEqual((db.query(BankLink).count(), db.query(BankAccount).count(), db.query(BankTransaction).count()), (0, 0, 0))
            self.assertEqual(db.query(FinanceAccount).count(), 1)


class SchedulerTests(unittest.TestCase):
    def test_pull_job_does_nothing_without_keys(self):
        with patch.dict(os.environ, {}, clear=False), patch.object(feed, "pull_all") as pull:
            os.environ.pop("PLAID_CLIENT_ID", None)
            asyncio.run(scheduler.bank_pull_job())
            pull.assert_not_called()

    def test_pull_job_can_be_switched_off(self):
        with patch.dict(os.environ, {**KEYS, "BANK_FEED_AUTO_PULL": "false"}), patch.object(feed, "pull_all") as pull:
            asyncio.run(scheduler.bank_pull_job())
            pull.assert_not_called()


class BankRouteTests(DbCase):
    def setUp(self):
        super().setUp()

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.patches = [patch.object(store, "SessionLocal", self.Session),
                        patch.object(finance, "phoenix_today", return_value=TODAY),
                        patch.dict(os.environ, KEYS)]
        for p in self.patches:
            p.start()
        store.clear_cache()
        self.read_only = app.state.read_only
        app.state.read_only = True  # production is read-only, and the money page must still save
        self.plaid = FakePlaid(CHASE_ACCOUNTS, [{"added": [plaid_txn("t1", "acc-chk", 20, "2026-11-22", "Parts")],
                                                 "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}])
        self.client_patch = patch.object(feed, "PlaidClient", return_value=self.plaid)
        self.client_patch.start()
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            db.commit()

    def tearDown(self):
        self.client_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        super().tearDown()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def connect_chase(self, owner):
        return owner.post("/finance/bank/connect", json={"public_token": "public-sandbox-1", "institution": "Chase"})

    def test_staff_and_strangers_are_kept_out(self):
        tech = self.sign_in("tech")
        for path in ("/finance/bank/pull", "/finance/bank/1/remove", "/finance/bank/accounts/1"):
            self.assertEqual(tech.post(path, data={}).status_code, 403)
        self.assertEqual(tech.post("/finance/bank/link-token", json={}).status_code, 403)
        self.assertEqual(tech.post("/finance/bank/connect", json={"public_token": "public-x"}).status_code, 403)
        self.assertEqual(TestClient(app, follow_redirects=False).get("/finance/bank/oauth").headers["location"], "/login")
        with self.Session() as db:
            self.assertEqual(db.query(BankLink).count(), 0)

    def test_page_offers_connect_and_lets_plaid_load(self):
        owner = self.sign_in()
        page = owner.get("/finance/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Connect a bank", page.text)
        self.assertIn("cdn.plaid.com", page.headers["content-security-policy"])
        self.assertIn("frame-ancestors 'none'", page.headers["content-security-policy"])
        self.assertEqual(owner.post("/finance/bank/link-token", json={}).json(), {"link_token": "link-sandbox-fixture"})

    def test_without_keys_the_page_explains_and_nothing_runs(self):
        with patch.dict(os.environ, {"PLAID_CLIENT_ID": "", "PLAID_SECRET": ""}):
            owner = self.sign_in()
            page = owner.get("/finance/")
            self.assertIn("needs the Plaid keys", page.text)
            self.assertNotIn("cdn.plaid.com", page.headers.get("content-security-policy", ""))
            self.assertEqual(owner.post("/finance/bank/link-token", json={}).status_code, 400)
            self.assertEqual(self.connect_chase(owner).status_code, 400)

    def test_connecting_a_bank_saves_it_encrypted_and_pulls_right_away(self):
        owner = self.sign_in()
        answer = self.connect_chase(owner)
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("Chase is connected", answer.json()["notice"])
        with self.Session() as db:
            link = db.query(BankLink).one()
            self.assertEqual(link.institution, "Chase")
            self.assertNotIn("access-sandbox-fixture-token", link.access_token_enc)
            self.assertEqual(feed.unseal(link.access_token_enc), "access-sandbox-fixture-token")
            self.assertEqual(db.query(BankTransaction).count(), 1)
        page = owner.get("/finance/")
        self.assertIn("Performance Business Checking", page.text)
        self.assertIn("From the bank", page.text)
        self.assertIn("owed $450.25", page.text)
        self.assertNotIn("access-sandbox-fixture-token", page.text)

    def test_a_bad_public_token_is_refused(self):
        owner = self.sign_in()
        self.assertEqual(owner.post("/finance/bank/connect", json={"public_token": "nope"}).status_code, 400)
        self.assertEqual(self.plaid.exchanged, [])

    def test_shop_personal_and_leave_out(self):
        owner = self.sign_in()
        self.connect_chase(owner)
        with self.Session() as db:
            checking = db.query(BankAccount).filter_by(plaid_account_id="acc-chk").one()
            account_id, cash_id = checking.id, checking.finance_account_id
        owner.post(f"/finance/bank/accounts/{account_id}", data={"scope": "personal", "tracked": "yes"})
        with self.Session() as db:
            self.assertEqual(db.get(FinanceAccount, cash_id).scope, "personal")
            self.assertEqual(db.get(BankAccount, account_id).scope, "personal")
        owner.post(f"/finance/bank/accounts/{account_id}", data={"scope": "personal", "tracked": "no"})
        with self.Session() as db:
            self.assertFalse(db.get(FinanceAccount, cash_id).active)
        bad = owner.post(f"/finance/bank/accounts/{account_id}", data={"scope": "joint", "tracked": "yes"})
        self.assertIn("problem=", bad.headers["location"])

    def test_update_now_and_disconnect(self):
        owner = self.sign_in()
        self.connect_chase(owner)
        pulled = owner.post("/finance/bank/pull", data={"window": "14"})
        self.assertIn("Updated+1+bank", pulled.headers["location"])
        with self.Session() as db:
            link_id = db.query(BankLink).one().id
        gone = owner.post(f"/finance/bank/{link_id}/remove", data={"window": "14"})
        self.assertIn("disconnected", gone.headers["location"])
        self.assertEqual(self.plaid.removed, ["access-sandbox-fixture-token"])
        with self.Session() as db:
            self.assertEqual(db.query(BankLink).count(), 0)
            self.assertEqual(db.query(FinanceAccount).count(), 1)

    def test_reconnect_uses_the_saved_bank(self):
        owner = self.sign_in()
        self.connect_chase(owner)
        with self.Session() as db:
            link = db.query(BankLink).one()
            link.status, link.last_error = "needs_login", "The bank wants you to sign in again. Tap Reconnect."
            db.commit()
            link_id = link.id
        self.assertIn("Reconnect", owner.get("/finance/").text)
        self.assertEqual(owner.post("/finance/bank/link-token", json={"link_id": link_id}).status_code, 200)
        done = owner.post("/finance/bank/connect", json={"link_id": link_id})
        self.assertEqual(done.status_code, 200, done.text)
        with self.Session() as db:
            self.assertEqual(db.query(BankLink).one().status, "ok")
        self.assertEqual(self.plaid.exchanged, ["public-sandbox-1"])  # no second exchange

    def test_bank_sign_in_return_page(self):
        page = self.sign_in().get("/finance/bank/oauth")
        self.assertEqual(page.status_code, 200)
        self.assertIn("imperialBankResume", page.text)
        self.assertIn("cdn.plaid.com", page.headers["content-security-policy"])


if __name__ == "__main__":
    unittest.main()
