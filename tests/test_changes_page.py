"""Owner's Changes page: open changes from GitHub, grouped, and his yes tied to the version he saw. Made-up data."""
import json
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app import change_review
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import User

PASSWORD = "fixture-password-1"
SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def pr(number, sha, title, body="", draft=True):
    return {"number": number, "title": title, "draft": draft, "html_url": f"https://github.com/x/y/pull/{number}",
            "head": {"ref": f"claude/{number}", "sha": sha}, "updated_at": "2026-11-25T17:00:00Z", "body": body}


RUNS = {
    SHA_A: [{"status": "completed", "conclusion": "success"}],
    SHA_B: [{"status": "completed", "conclusion": "failure"}],
    SHA_C: [{"status": "in_progress", "conclusion": None}],
}


def fake_github(path, params=None):
    if path.endswith("/pulls") and params["state"] == "open":
        return [pr(249, SHA_A, "Booking site launch", "Before: old.\n\nAfter: The booking page shows the launch banner."),
                pr(237, SHA_B, "Estimate tools"), pr(229, SHA_C, "Bank feed")]
    if path.endswith("/pulls"):
        return [{"number": 200, "title": "Old fix", "html_url": "https://github.com/x/y/pull/200",
                 "merged_at": "2099-01-01T00:00:00Z"},
                {"number": 199, "title": "Closed unmerged", "html_url": "u", "merged_at": None}]
    sha = path.split("/commits/")[1].split("/")[0]
    return {"check_runs": RUNS[sha]}


class ChangesPageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.patches = [patch.object(store, "SessionLocal", self.Session),
                        patch.object(change_review, "_get", side_effect=fake_github)]
        for p in self.patches:
            p.start()
        store.clear_cache()
        change_review.clear_cache()
        self.read_only = app.state.read_only
        app.state.read_only = True  # production runs read-only; the owner's decision must still save
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.add(User(id=3, username="boss", password_hash=hash_password(PASSWORD), role="admin"))
            db.commit()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        store.clear_cache()
        change_review.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def saved(self):
        with self.Session() as db:
            row = db.get(store.BusinessSetting, change_review.SETTING_KEY)
            return json.loads(row.value) if row else {}

    def test_page_groups_changes_by_what_they_need(self):
        page = self.sign_in().get("/changes")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertIn("Waiting on your yes", text)
        self.assertIn("Needs a fix first", text)
        self.assertIn("Still being worked on", text)
        self.assertIn("The booking page shows the launch banner.", text)
        self.assertLess(text.index("Booking site launch"), text.index("Estimate tools"))
        self.assertIn("Merged this week", text)
        self.assertIn("Old fix", text)
        self.assertNotIn("Closed unmerged", text)
        self.assertIn('href="/changes"', text)  # in the menu

    def test_only_the_owner_sees_or_approves(self):
        for name in ("office", "boss"):
            client = self.sign_in(name)
            self.assertEqual(client.get("/changes").status_code, 403)
            self.assertEqual(client.post("/changes/249", data={"decision": "approved", "sha": SHA_A}).status_code, 403)
        self.assertEqual(TestClient(app).get("/changes").status_code, 401)
        self.assertEqual(self.saved(), {})

    def test_approval_is_recorded_for_the_version_seen(self):
        owner = self.sign_in()
        done = owner.post("/changes/249", data={"decision": "approved", "sha": SHA_A, "note": "looks good"})
        self.assertEqual(done.status_code, 303)
        self.assertEqual(self.saved()["249"]["decision"], "approved")
        self.assertEqual(self.saved()["249"]["sha"], SHA_A)
        self.assertEqual(self.saved()["249"]["note"], "looks good")
        text = owner.get("/changes").text
        self.assertIn("You approved", text)
        self.assertNotIn("Waiting on your yes", text)
        with self.Session() as db:  # kept with history
            self.assertEqual(db.query(store.BusinessSettingChange).filter_by(key="change_approvals").count(), 1)

    def test_update_after_approval_asks_again(self):
        owner = self.sign_in()
        owner.post("/changes/249", data={"decision": "approved", "sha": "d" * 40})
        text = owner.get("/changes").text
        self.assertIn("Changed since you approved", text)

    def test_not_now_and_undo(self):
        owner = self.sign_in()
        owner.post("/changes/237", data={"decision": "not_now", "sha": SHA_B})
        self.assertIn("Set aside", owner.get("/changes").text)
        owner.post("/changes/237", data={"decision": "clear"})
        self.assertNotIn("237", self.saved())

    def test_bad_decision_is_refused(self):
        owner = self.sign_in()
        self.assertEqual(owner.post("/changes/249", data={"decision": "merge", "sha": SHA_A}).status_code, 422)
        self.assertEqual(owner.post("/changes/249", data={"decision": "approved", "sha": "nope"}).status_code, 422)
        self.assertEqual(self.saved(), {})

    def test_github_down_shows_a_message_not_an_error(self):
        with patch.object(change_review, "_get", side_effect=httpx.ConnectError("down")):
            change_review.clear_cache()
            page = self.sign_in().get("/changes")
        self.assertEqual(page.status_code, 200)
        self.assertIn("reach GitHub just now", page.text)

    def test_list_is_cached_between_visits(self):
        owner = self.sign_in()
        owner.get("/changes")
        calls = change_review._get.call_count
        owner.get("/changes")
        self.assertEqual(change_review._get.call_count, calls)
        owner.get("/changes?refresh=1")
        self.assertGreater(change_review._get.call_count, calls)

    def test_decision_list_for_the_assistant(self):
        self.sign_in().post("/changes/249", data={"decision": "approved", "sha": SHA_A})
        store.clear_cache()
        rows = change_review.decision_list()
        self.assertEqual(rows[0]["number"], 249)
        self.assertEqual(rows[0]["decision"], "approved")


class SummaryTests(unittest.TestCase):
    def test_after_paragraph_is_used(self):
        self.assertEqual(change_review._summary("Before: x\n\n**After:** Customers see y."), "Customers see y.")

    def test_first_plain_paragraph_otherwise(self):
        self.assertEqual(change_review._summary("## Summary\n\nAdds a page."), "Adds a page.")

    def test_checks_state(self):
        self.assertEqual(change_review.checks_state([]), "none")
        self.assertEqual(change_review.checks_state([{"status": "completed", "conclusion": "skipped"}]), "passed")


if __name__ == "__main__":
    unittest.main()
