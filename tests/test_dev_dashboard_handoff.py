"""Synthetic coverage for the shared development card."""
import unittest
from unittest.mock import patch

import test_staff_accounts as staff
from app.dev_work_model import DevWorkJob
from app.imperial_task_model import ImperialTask
from app.routes import internal_messaging


class DevelopmentDashboardTests(unittest.TestCase):
    setUp = staff.StaffLoginTests.setUp
    tearDown = staff.StaffLoginTests.tearDown
    sign_in = staff.StaffLoginTests.sign_in

    @patch.dict("os.environ", {"DEV_COORDINATOR_CAPTURE_ENABLED": "true", "IMPERIAL_TASKS_ENABLED": "true"})
    @patch.object(internal_messaging, "INTERNAL_API_KEY", "fixture-key")
    def test_retry_shows_one_shared_task_to_owner_only(self):
        payload = {"messageId": "synthetic-dev", "projectKey": "website",
                   "instruction": "Review the synthetic website accessibility checklist"}
        def submit():
            response = self.client.post("/internal/imperial/dev-work/submit", json=payload,
                                        headers={"X-Internal-Key": "fixture-key"})
            self.assertEqual(response.status_code, 200)
            return response.json()

        first = submit()
        with self.Session() as db:
            job = db.get(DevWorkJob, first["id"])
            job.status = "blocked"
            job.latest_summary = "Synthetic review is waiting for test evidence."
            db.add(ImperialTask(id=160, title="Synthetic development task",
                                source="dev_work", external_id=str(job.id)))
            db.commit()
        replay = submit()
        self.assertTrue(replay["duplicate"])
        self.assertEqual(first["id"], replay["id"])

        owner, _ = self.sign_in()
        for url in ("/dashboard", "/my-day"):
            with self.subTest(url=url):
                response = owner.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text.count('data-task-id="160"'), 1)
                self.assertEqual(response.text.count("Synthetic development task"), 1)
                self.assertIn("Blocked", response.text)
                self.assertIn("Synthetic review is waiting for test evidence.", response.text)

        crew, _ = self.sign_in("crew")
        response = crew.get("/my-day")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Synthetic review", response.text)
        self.assertNotIn('data-task-id="160"', response.text)
