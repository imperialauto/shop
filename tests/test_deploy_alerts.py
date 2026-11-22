"""Railway crash alerts reach Home and My Day, and clear on the next good deploy. Made-up data."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deploy_alerts, system_health
from app.database import Base, get_db
from app.dispatch_model import DispatchSync
from app.imperial_task_model import ImperialTask
from app.main import app

TOKEN = "fixture-railway-token-0123456789"
NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


def event(kind, service="web", ephemeral=False):
    return {"type": kind, "details": {"status": kind.rsplit(".", 1)[-1].upper()},
            "resource": {"service": {"id": "svc", "name": service},
                         "environment": {"id": "env", "name": "production", "isEphemeral": ephemeral},
                         "deployment": {"id": "dep"}},
            "severity": "WARNING", "timestamp": "2026-11-09T15:00:00Z"}


class DeployAlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add_all([DispatchSync(source="quo_recovery", last_success_at=NOW - timedelta(minutes=4), consecutive_failures=0),
                        DispatchSync(source="backup", last_success_at=NOW - timedelta(hours=4), consecutive_failures=0)])
            db.commit()

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        patcher = patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true", deploy_alerts.TOKEN_ENV: TOKEN})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def post(self, payload, token=TOKEN):
        return self.client.post(f"/webhooks/railway/{token}", json=payload)

    def health(self):
        with self.Session() as db:
            return system_health.report(db, NOW)

    def tasks(self):
        with self.Session() as db:
            return [(t.title, t.status, t.priority) for t in db.query(ImperialTask).filter_by(source="railway")]

    def test_wrong_token_is_not_found_and_records_nothing(self):
        self.assertEqual(self.post(event("Deployment.crashed"), token="wrong-token-0123456789abcdef").status_code, 404)
        self.assertEqual(self.tasks(), [])

    def test_no_token_set_means_off(self):
        with patch.dict(os.environ, {deploy_alerts.TOKEN_ENV: ""}):
            self.assertEqual(self.post(event("Deployment.crashed")).status_code, 404)

    def test_health_line_hidden_until_railway_reports(self):
        self.assertNotIn("App deploys", [c["name"] for c in self.health()["checks"]])

    def test_crash_shows_on_home_and_my_day(self):
        response = self.post(event("Deployment.crashed"))
        self.assertEqual(response.json(), {"status": "ok", "service": "web", "state": "bad"})
        report = self.health()
        self.assertFalse(report["ok"])
        problem = [c for c in report["problems"] if c["name"] == "App deploys"][0]
        self.assertIn("The web app crashed.", problem["detail"])
        self.assertEqual(self.tasks(), [("Fix the web app: Railway says it went down", "open", "high")])

    def test_repeat_crash_keeps_one_task(self):
        self.post(event("Deployment.crashed"))
        self.post(event("Deployment.failed"))
        self.assertEqual(len(self.tasks()), 1)

    def test_good_deploy_clears_home_and_closes_task(self):
        self.post(event("Deployment.failed", service="shop"))
        self.post(event("Deployment.deployed", service="shop"))
        report = self.health()
        self.assertTrue(report["ok"])
        self.assertIn("App deploys", [c["name"] for c in report["checks"]])
        self.assertEqual(self.tasks()[0][1], "done_verified")

    def test_crash_after_recovery_reopens_the_same_task(self):
        self.post(event("Deployment.crashed"))
        self.post(event("Deployment.deployed"))
        self.post(event("Deployment.crashed"))
        self.assertEqual([t[1] for t in self.tasks()], ["open"])

    def test_preview_builds_and_other_events_are_ignored(self):
        self.assertEqual(self.post(event("Deployment.crashed", ephemeral=True)).json(), {"status": "ignored"})
        self.assertEqual(self.post(event("Deployment.building")).json(), {"status": "ignored"})
        self.assertEqual(self.tasks(), [])

    def test_older_payload_format(self):
        payload = {"type": "DEPLOY", "status": "CRASHED", "service": {"name": "web"}, "environment": {"name": "production"}}
        self.assertEqual(self.post(payload).json()["state"], "bad")

    def test_task_storage_off_still_shows_on_home(self):
        with patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": ""}):
            self.post(event("Deployment.crashed"))
        self.assertEqual(self.tasks(), [])
        self.assertFalse(self.health()["ok"])


if __name__ == "__main__":
    unittest.main()
