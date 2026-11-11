"""Imperial development work intake: synthetic data, no live model, Quo or GitHub calls."""
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.dev_work_model import DevWorkEvent, DevWorkJob
from app.main import app
from app.routes import dev_work, internal_messaging


class DevelopmentWorkTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.old_read_only = app.state.read_only
        app.state.read_only = False

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.key_patch = patch.object(internal_messaging, "INTERNAL_API_KEY", "test-secret-key")
        self.key_patch.start()
        self.flags = patch.dict(os.environ, {"DEV_COORDINATOR_CAPTURE_ENABLED": "true"})
        self.flags.start()
        self.client = TestClient(app)
        self.headers = {"X-Internal-Key": "test-secret-key"}

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.old_read_only
        self.flags.stop()
        self.key_patch.stop()
        self.engine.dispose()

    def submit(self, message="owner-sms-unique", **kwargs):
        values = {"messageId": message, "projectKey": "website",
                  "instruction": "Continue working on the website acceptance checklist",
                  "modelPreference": "auto"}
        values.update(kwargs)
        return self.client.post("/internal/imperial/dev-work/submit", headers=self.headers, json=values)

    def test_feature_disabled_refuses_new_work(self):
        with patch.dict(os.environ, {"DEV_COORDINATOR_CAPTURE_ENABLED": "false"}):
            self.assertEqual(self.submit().status_code, 503)
        with self.Session() as db:
            self.assertEqual(db.query(DevWorkJob).count(), 0)

    def test_recovery_read_only_refuses_writes(self):
        app.state.read_only = True
        self.assertEqual(self.submit().status_code, 503)
        with self.Session() as db:
            self.assertEqual(db.query(DevWorkJob).count(), 0)

    def test_owner_internal_key_is_required(self):
        self.assertEqual(self.client.post("/internal/imperial/dev-work/submit", json={
            "messageId": "test-1", "projectKey": "website",
            "instruction": "Continue website tests",
        }).status_code, 401)
        self.assertEqual(self.client.get("/internal/imperial/dev-work/jobs").status_code, 401)

    def test_queued_job_is_durable_idempotent_and_audited(self):
        first = self.submit()
        again = self.submit(instruction="This replay MUST NOT change the original instruction")
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        data, duplicate = first.json(), again.json()
        self.assertEqual(data["id"], duplicate["id"])
        self.assertFalse(data["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(data["status"], "queued")
        self.assertFalse(data["executionStarted"])
        self.assertFalse(data["workerConnected"])
        with self.Session() as db:
            jobs = db.query(DevWorkJob).all()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(len(db.query(DevWorkEvent).all()), 1)
            self.assertIn("acceptance checklist", jobs[0].instruction)
            self.assertEqual(jobs[0].source, "quo_owner")
            self.assertEqual(jobs[0].status, "queued")
            self.assertIsNone(jobs[0].assigned_worker)

    def test_live_or_customer_affecting_instruction_is_held_for_approval(self):
        for number, instruction in enumerate((
            "Continue working on the website and deploy to production",
            "Continue working on the dashboard and send a customer text",
            "Continue working on finance and delete account records",
        ), start=1):
            response = self.submit(message=f"risky-{number}", instruction=instruction)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "needs_approval")
            self.assertFalse(response.json()["executionStarted"])

    def test_reject_unscoped_project_bad_model_and_invalid_message_id(self):
        self.assertEqual(self.submit(projectKey="../../other").status_code, 422)
        self.assertEqual(self.submit(modelPreference="free-runner").status_code, 422)
        self.assertEqual(self.submit(message="../bad").status_code, 422)
        self.assertEqual(self.submit(instruction="   ").status_code, 422)
        with self.Session() as db:
            self.assertEqual(db.query(DevWorkJob).count(), 0)

    def test_list_and_dashboard_report_real_status_and_limits(self):
        self.assertEqual(self.submit().status_code, 200)
        result = self.client.get("/internal/imperial/dev-work/jobs", headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(result.json()["jobs"]), 1)
        self.assertIn("not all GitHub", result.json()["coverage"])
        self.assertFalse(result.json()["workerConnected"])
        self.assertEqual(self.client.get("/internal/imperial/dev-work/jobs?limit=26",
                                         headers=self.headers).status_code, 422)
        with self.Session() as db:
            owner = dev_work.dashboard_snapshot(db, "owner")
            self.assertEqual(owner["jobs"][0]["status"], "queued")
            self.assertFalse(owner["workerConnected"])
            self.assertFalse(dev_work.dashboard_snapshot(db, "office")["enabled"])
            self.assertFalse(dev_work.dashboard_snapshot(db, "tech")["enabled"])
            with patch.dict(os.environ, {"DEV_COORDINATOR_CAPTURE_ENABLED": "false"}):
                self.assertFalse(dev_work.dashboard_snapshot(db, "owner")["enabled"])


if __name__ == "__main__":
    unittest.main()
