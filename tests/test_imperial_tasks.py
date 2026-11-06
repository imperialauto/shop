"""Feature-gated Imperial owner task tests; synthetic SQLite, no outbound calls."""
import os
import unittest
from unittest.mock import patch
import fixture_environment  # noqa

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.imperial_task_model import ImperialTask, ImperialTaskAudit
from app.main import app
from app.routes import internal_messaging


class TaskCaptureTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.old_read_only = app.state.read_only
        self.key_patch = patch.object(internal_messaging, "INTERNAL_API_KEY", "test-internal-secret")
        self.key_patch.start()
        self.flag_patch = patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"})
        self.flag_patch.start()
        self.client = TestClient(app)
        self.auth = {"X-Internal-Key": "test-internal-secret"}

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.old_read_only
        self.key_patch.stop()
        self.flag_patch.stop()
        self.engine.dispose()

    def capture(self, event="test-1", title="Verify the website intake alerts", **kwargs):
        return self.client.post("/internal/imperial/tasks/capture", headers=self.auth,
            json={"messageId": event, "title": title, **kwargs})

    def test_disabled_gate_refuses_writes_even_outside_recovery(self):
        app.state.read_only = False
        with patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "false"}):
            response = self.capture()
        self.assertEqual(response.status_code, 503)
        with self.Session() as db:
            self.assertEqual(db.query(ImperialTask).count(), 0)

    def test_recovery_blocks_unapproved_task_writes(self):
        app.state.read_only = True
        self.assertEqual(self.capture().status_code, 503)
        with self.Session() as db:
            self.assertEqual(db.query(ImperialTask).count(), 0)

    def test_internal_key_is_required_even_when_write_enabled(self):
        app.state.read_only = False
        self.assertEqual(self.client.post("/internal/imperial/tasks/capture", json={
            "messageId": "t1", "title": "Check customer request",
        }).status_code, 401)
        self.assertEqual(self.client.get("/internal/imperial/tasks/open").status_code, 401)

    def test_capture_is_idempotent_and_audited(self):
        app.state.read_only = False
        first = self.capture(projectKey="website")
        again = self.capture(title="This replay cannot overwrite existing task")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(again.json()["duplicate"])
        with self.Session() as db:
            tasks = db.query(ImperialTask).all()
            audits = db.query(ImperialTaskAudit).all()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(len(audits), 1)
            self.assertEqual((audits[0].actor, audits[0].action), ("owner_sms", "created"))
            self.assertEqual(tasks[0].project_key, "website")

    def test_open_tasks_is_read_only_and_gives_coverage_label(self):
        app.state.read_only = False
        self.capture()
        app.state.read_only = True
        response = self.client.get("/internal/imperial/tasks/open", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["tasks"]), 1)
        self.assertIn("Imperial internal tasks only", response.json()["coverage"])
        self.assertNotIn("customer", str(response.json()["tasks"][0]).lower())

    def test_invalid_task_does_not_create_row(self):
        app.state.read_only = False
        self.assertEqual(self.capture(title="  ").status_code, 422)
        self.assertEqual(self.capture(projectKey="../../secret").status_code, 422)
        self.assertEqual(self.capture(event="").status_code, 422)
        with self.Session() as db:
            self.assertEqual(db.query(ImperialTask).count(), 0)


if __name__ == "__main__":
    unittest.main()
