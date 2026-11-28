"""Ready-for-pickup text when a job is marked Complete. Made-up data, private database, nothing is sent."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401

from app import customer_file, job_events, pickup_texts, security_controls
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Invoice, LineItem, Payment, RepairOrder
from app.routes import dispatch

import test_staff_accounts as base  # module import, so its own tests aren't collected twice


def thread(path, phone):
    assert (path, phone) == ("/conversation", "6025550100")
    return {"messages": [{"id": "AC1", "direction": "INBOUND"}, {"id": "AC7", "direction": "OUTBOUND"}]}, None


def broken(path, phone):
    return None, "unavailable"


class PickupTextTests(unittest.TestCase):
    setUp = base.StaffLoginTests.setUp
    tearDown = base.StaffLoginTests.tearDown
    sign_in = base.StaffLoginTests.sign_in
    make_job = base.StaffLoginTests.make_job

    def job(self, total=458.20, paid=0.0, invoice=True):
        job = self.make_job()
        with self.Session() as db:
            ro = db.get(RepairOrder, job)
            ro.status = "in_progress"
            db.add(LineItem(repair_order_id=job, description="Clutch kit", item_type="part", unit_price=total, approval="approved"))
            if invoice:
                inv = Invoice(repair_order_id=job, total=total, status="sent")
                db.add(inv)
                db.flush()
                if paid:
                    db.add(Payment(invoice_id=inv.id, amount=paid, method="zelle"))
            db.commit()
        return job

    def move(self, job, status, on=True, fetch=thread, terms=False):
        owner, _ = self.sign_in()
        real = security_controls.value
        with patch.object(pickup_texts, "enabled", lambda: on), patch.object(customer_file, "_bridge", fetch), \
             patch.object(security_controls, "value", lambda key: terms if key == "terms_on_approval_page" else real(key)):
            self.assertEqual(owner.post(f"/ro/{job}/update-status", data={"status": status}).status_code, 302)

    def pickups(self, job):
        with self.Session() as db:
            return [(i.status, i.draft_reply) for i in
                    db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == f"PICKUP{job}").all()]

    def test_off_by_default_sends_nothing(self):
        job = self.job()
        self.move(job, "complete", on=False)
        self.assertEqual(self.pickups(job), [])
        self.assertFalse(pickup_texts.enabled())

    def test_complete_queues_one_text_with_the_balance(self):
        job = self.job(total=458.20, paid=229.10)
        self.move(job, "complete")
        [(status, text)] = self.pickups(job)
        self.assertEqual(status, "approved")
        self.assertIn("Hi Pat, your 2016 Ford Focus is done and ready for pickup.", text)
        self.assertIn("The balance due is $229.10, by Zelle to jaelan@imperialautocarellc.com, Apple Pay, cash, or card with a QuickBooks link I can text you.", text)
        self.assertNotIn("Storage", text)  # only where the customer agreed to the terms
        self.assertNotIn("Snap", text)
        self.assertTrue(text.endswith("Jae"))
        with self.Session() as db:
            replies = dispatch.queued_replies(db)
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["messageId"], "AC7")

    def test_storage_line_only_with_signed_terms(self):
        job = self.job()
        self.move(job, "complete", terms=True)
        [(_, text)] = self.pickups(job)
        self.assertIn(pickup_texts.STORAGE_LINE + " Jae", text)

    def test_paid_in_full(self):
        job = self.job(total=300, paid=300)
        self.move(job, "complete")
        [(_, text)] = self.pickups(job)
        self.assertIn("You're all paid up.", text)
        self.assertNotIn("balance due", text)

    def test_no_invoice_waits_for_the_owner(self):
        job = self.job(invoice=False)
        self.move(job, "complete")
        [(status, text)] = self.pickups(job)
        self.assertEqual(status, "needs_owner")
        self.assertNotIn("$", text)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_unreadable_thread_waits_for_the_owner(self):
        job = self.job()
        self.move(job, "complete", fetch=broken)
        [(status, _)] = self.pickups(job)
        self.assertEqual(status, "needs_owner")
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)

    def test_one_text_per_job_even_if_it_moves_back_and_forth(self):
        job = self.job()
        self.move(job, "complete")
        self.move(job, "in_progress")
        self.move(job, "complete")
        self.assertEqual(len(self.pickups(job)), 1)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 1)

    def test_other_statuses_send_nothing(self):
        job = self.job()
        self.move(job, "waiting_parts")
        self.assertEqual(self.pickups(job), [])

    def test_a_failure_never_blocks_the_status_change(self):
        job = self.job()

        def boom(path, phone):
            raise RuntimeError("bridge down")
        self.move(job, "complete", fetch=boom)
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, job).status, "complete")
        self.assertEqual(self.pickups(job), [])

    def test_pickup_hooks_send_one_text_each(self):
        from app import review_requests, service_checkins
        self.assertEqual(job_events.HANDLERS["complete"], [pickup_texts.queue])
        # Review the day after pickup, check-in a week after: each schedules its own later text.
        self.assertEqual(job_events.HANDLERS["delivered"], [review_requests.schedule, service_checkins.schedule])
        job = self.job()
        self.move(job, "complete", terms=True)
        self.assertNotIn("review", self.pickups(job)[0][1].lower())


if __name__ == "__main__":
    unittest.main()
