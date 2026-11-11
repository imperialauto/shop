"""One job, one task: a website item closes when its job moves forward, and Home shows the job's next step."""
import fixture_environment  # noqa: F401

from app.dispatch_model import DispatchAudit, DispatchItem
from app.models import RepairOrder
from app import website_alerts
import unittest

import test_staff_accounts as base  # module import, so its own tests aren't collected twice


class OneTaskPerJob(unittest.TestCase):
    setUp = base.StaffLoginTests.setUp
    tearDown = base.StaffLoginTests.tearDown
    sign_in = base.StaffLoginTests.sign_in
    make_job = base.StaffLoginTests.make_job

    def website_item(self, job_id, kind="request"):
        with self.Session() as db:
            item = website_alerts.queue(db, db.get(RepairOrder, job_id), kind, "Made-up website request.")
            db.commit()
            return item.id

    def status(self, item_id):
        with self.Session() as db:
            return db.get(DispatchItem, item_id).status

    def test_status_change_closes_the_website_item_once(self):
        job = self.make_job()
        item = self.website_item(job)
        owner, _ = self.sign_in()
        owner.post(f"/ro/{job}/update-status", data={"status": "intake"})
        self.assertEqual(self.status(item), "needs_owner")  # still untouched
        owner.post(f"/ro/{job}/update-status", data={"status": "diagnosing"})
        self.assertEqual(self.status(item), "closed")
        with self.Session() as db:
            audit = db.query(DispatchAudit).filter(DispatchAudit.item_id == item,
                                                   DispatchAudit.action == "website_item_closed_by_job").one()
            self.assertEqual(audit.actor, "owner")

    def test_other_jobs_and_decision_items_stay_open(self):
        job, other = self.make_job(), self.make_job()
        mine, theirs = self.website_item(job), self.website_item(other)
        with self.Session() as db:
            decision = website_alerts.queue_decision(db, db.get(RepairOrder, job), []).id
            db.commit()
        owner, _ = self.sign_in()
        owner.post(f"/ro/{job}/update-status", data={"status": "diagnosing"})
        self.assertEqual((self.status(mine), self.status(theirs), self.status(decision)), ("closed", "needs_owner", "needs_owner"))

    def test_home_shows_the_same_next_step_as_the_job(self):
        job = self.make_job()
        owner, _ = self.sign_in()
        page = owner.get("/dashboard").text
        self.assertIn("Next step", page)
        self.assertIn("Add the repairs and prices", page)
        self.assertNotIn("Made-up concern", page)
