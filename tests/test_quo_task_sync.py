"""Quo tasks in Master Tasks, both directions. Synthetic SQLite; no Quo or customer contact."""
import os
import unittest
from unittest.mock import patch
import fixture_environment  # noqa

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.imperial_task_model import ImperialTask
from app.main import app
from app import quo_task_sync
from app.routes import internal_messaging
from app.routes.imperial_tasks import OwnerTaskChange, change_task


def quo(task_id, title, status="open", conversation="CN1", **extra):
    return {"id": task_id, "title": title, "status": status, "conversationId": conversation,
            "createdAt": "2026-11-14T20:01:30.339Z", **extra}


class QuoTaskSyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.old_read_only = app.state.read_only
        app.state.read_only = False
        self.patches = [patch.object(internal_messaging, "INTERNAL_API_KEY", "test-internal-secret"),
                        patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"})]
        for p in self.patches:
            p.start()
        self.client = TestClient(app)
        self.auth = {"X-Internal-Key": "test-internal-secret"}

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.old_read_only
        for p in self.patches:
            p.stop()
        self.engine.dispose()

    def post(self, tasks):
        return self.client.post("/internal/imperial/tasks/quo-sync", headers=self.auth, json={"tasks": tasks})

    def test_requires_internal_key(self):
        self.assertEqual(self.client.post("/internal/imperial/tasks/quo-sync", json={"tasks": []}).status_code, 401)

    def test_tasks_saved_once_and_titles_update(self):
        body = self.post([quo("TK1", "[REPLY DRAFT] Suburban — (623) 204-9200"), quo("TK1", "dup")]).json()
        self.assertEqual(body["added"], 1)
        self.post([quo("TK1", "[REPLY DRAFT — UPDATE] Suburban Monday — (623) 204-9200")])
        with self.Session() as db:
            rows = db.query(ImperialTask).all()
            self.assertEqual(len(rows), 1)
            self.assertIn("Monday", rows[0].title)
            self.assertEqual(quo_task_sync.details(rows[0])["phone"], "+16232049200")
            self.assertEqual(rows[0].status, "open")

    def test_done_in_quo_closes_here(self):
        self.post([quo("TK1", "[CALL BACK] x")])
        body = self.post([quo("TK1", "[CALL BACK] x", status="completed")]).json()
        self.assertEqual(body["closed"], 1)
        with self.Session() as db:
            self.assertEqual(db.query(ImperialTask).one().status, "done_verified")

    def test_done_here_is_sent_to_quo_until_acknowledged(self):
        self.post([quo("TK1", "[NEW LEAD] y")])
        with self.Session() as db:
            task = db.query(ImperialTask).one()
            change_task(db, task.id, OwnerTaskChange(messageId="m1", operation="complete"), "owner:1")
        body = self.post([quo("TK1", "[NEW LEAD] y")]).json()
        self.assertEqual(body["complete_in_quo"], ["TK1"])
        with self.Session() as db:
            self.assertEqual(db.query(ImperialTask).one().status, "done_verified")  # Quo "open" doesn't reopen it
        ack = self.client.post("/internal/imperial/tasks/quo-sync/ack", headers=self.auth, json={"completed": ["TK1"]})
        self.assertEqual(ack.json()["acknowledged"], 1)
        self.assertEqual(self.post([quo("TK1", "[NEW LEAD] y", status="completed")]).json()["complete_in_quo"], [])

    def test_reopened_here_stays_open_after_quo_finished(self):
        self.post([quo("TK1", "[ADMIN] z", status="completed")])
        with self.Session() as db:
            task = db.query(ImperialTask).one()
            change_task(db, task.id, OwnerTaskChange(messageId="m2", operation="reopen"), "owner:1")
        self.post([quo("TK1", "[ADMIN] z", status="completed")])
        with self.Session() as db:
            self.assertEqual(db.query(ImperialTask).one().status, "open")

    def test_wait_operation(self):
        self.post([quo("TK1", "[AWAITING CUSTOMER] w")])
        with self.Session() as db:
            task = db.query(ImperialTask).one()
            change_task(db, task.id, OwnerTaskChange(messageId="m3", operation="wait"), "owner:1")
            self.assertEqual(db.get(ImperialTask, task.id).status, "blocked")

    def test_label(self):
        self.assertEqual(quo_task_sync.label("[REPLY DRAFT — UPDATE] Hellcat clips")["kind"], "Reply")
        self.assertTrue(quo_task_sync.label("[REPLY DRAFT — UPDATE] Hellcat")["update"])
        self.assertEqual(quo_task_sync.label("Plain title")["text"], "Plain title")
        self.assertTrue(quo_task_sync.label("[P1 PRICE CORRECTION REVIEW] a")["urgent"])


if __name__ == "__main__":
    unittest.main()
