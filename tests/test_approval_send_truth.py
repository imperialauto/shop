"""An approval text counts as sent only when Quo confirmed it (the job 21 bug: the dashboard said
"already went out" for a text that never reached the customer). Covers the owner's Send it again,
the copy-link box, a text that never got an answer, and the shop line reporting Quo's id.
Made-up data, private database."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_job_flow
from test_dispatch import HEADERS, KEY

from app import approval_links, customer_file
from app.dispatch_model import DispatchAudit, DispatchOutbound
from app.models import RepairOrder
from app.routes import internal_messaging

thread = test_job_flow.thread


class ApprovalSendTruthTests(test_job_flow.JobFlowTests):
    def press(self, path="text-link-now", client=None):
        with patch.object(customer_file, "_bridge", thread):
            return (client or self.owner).post(f"/ro/3/{path}")

    def rows(self):
        with self.Session() as db:
            return [(o.id, o.status) for o in db.query(DispatchOutbound).order_by(DispatchOutbound.id)]

    def set_row(self, row_id, status, minutes_ago=0):
        with self.Session() as db:
            row = db.get(DispatchOutbound, row_id)
            row.status = status
            when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
            row.created_at = row.updated_at = when
            db.commit()

    def last(self):
        with self.Session() as db:
            return approval_links.last_send(db, db.get(RepairOrder, 3))

    def test_a_failed_or_held_back_text_can_be_sent_again(self):
        for status in ("failed", "skipped"):
            with self.subTest(status=status):
                self.press()
                row = self.rows()[-1][0]
                self.set_row(row, status)
                self.assertEqual(self.last()["state"], "not_sent")
                page = self.owner.get("/ro/3").text
                self.assertIn("Approval text not sent", page)
                self.assertIn("Send it again", page)
                again = self.press()
                self.assertIn("link=sending", again.headers["location"])
                self.assertEqual(self.rows()[-1][1], "queued")
                self.set_row(self.rows()[-1][0], "failed")

    def test_only_a_confirmed_text_counts_as_already_and_the_owner_can_send_it_again(self):
        self.press()
        self.set_row(self.rows()[0][0], "sent")
        self.assertEqual(self.last()["state"], "sent")
        again = self.press()
        self.assertIn("link=already", again.headers["location"])
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("Approval text sent", self.owner.get("/ro/3").text)
        # The customer says they never got it: the owner sends it again.
        resent = self.press("resend-link")
        self.assertIn("link=sending", resent.headers["location"])
        self.assertEqual([s for _, s in self.rows()], ["sent", "queued"])
        # Pressing again while that one is still going out never doubles it.
        twice = self.press("resend-link")
        self.assertIn("link=in_flight", twice.headers["location"])
        self.assertEqual(len(self.rows()), 2)
        with self.Session() as db:
            self.assertTrue(db.query(DispatchAudit).filter(
                DispatchAudit.detail.like("%Send it again%")).count())

    def test_only_the_owner_can_send_it_again(self):
        self.press()
        self.set_row(self.rows()[0][0], "sent")
        self.assertEqual(self.press("resend-link", self.office).status_code, 403)
        self.assertEqual(self.press("resend-link", self.tech).status_code, 403)
        self.assertEqual(len(self.rows()), 1)
        for client in (self.office, self.tech):
            page = client.get("/ro/3").text
            self.assertNotIn('id="approval-text"', page)
            self.assertNotIn("Send it again", page)

    def test_a_text_the_shop_line_never_answered_counts_as_not_sent(self):
        self.press()
        row = self.rows()[0][0]
        self.set_row(row, "reserved", minutes_ago=30)
        last = self.last()
        self.assertEqual(last["state"], "not_sent")
        self.assertIn("never confirmed", last["why"])
        again = self.press()
        self.assertIn("link=sending", again.headers["location"])
        self.assertEqual([s for _, s in self.rows()], ["failed", "queued"])
        with self.Session() as db:
            self.assertEqual(db.query(DispatchAudit).filter(DispatchAudit.action == "text_expired").count(), 1)

    def test_a_fresh_text_is_shown_as_waiting_and_not_sent_twice(self):
        self.press()
        self.assertEqual(self.last()["state"], "waiting")
        page = self.owner.get("/ro/3").text
        self.assertIn("with the shop line", page)
        self.assertNotIn("Send it again", page)
        self.assertIn("link=in_flight", self.press("resend-link").headers["location"])
        self.assertIn("link=in_flight", self.press().headers["location"])
        self.assertEqual(len(self.rows()), 1)

    def test_the_owner_can_copy_the_approval_link(self):
        page = self.owner.get("/ro/3").text
        self.assertIn("/sign/ro/flow-token", page)
        self.assertIn("Copy link", page)
        self.assertNotIn("Copy link", self.tech.get("/ro/3").text)

    def test_closed_jobs_cannot_be_texted_again(self):
        with self.Session() as db:
            db.get(RepairOrder, 3).status = "delivered"
            db.commit()
        self.assertEqual(self.press("resend-link").status_code, 409)
        self.assertEqual(self.rows(), [])

    def test_the_shop_line_reports_quo_id_and_a_late_send_is_recorded(self):
        self.press()
        with self.Session() as db:
            key = db.query(DispatchOutbound).first().dedupe_key
        self.set_row(self.rows()[0][0], "reserved", minutes_ago=30)
        self.press()  # gives up on the first one and sends again
        with patch.object(internal_messaging, "INTERNAL_API_KEY", KEY):
            r = self.customer_client.post("/internal/dispatch/outbound", headers=HEADERS,
                                          json={"key": key, "status": "sent", "messageId": "AC123"})
        self.assertEqual(r.status_code, 200, r.text)
        with self.Session() as db:
            actions = {a.action: a.detail for a in db.query(DispatchAudit)}
        self.assertIn("text_sent_late", actions)
        self.assertIn("AC123", actions["text_sent"])

    def test_connector_returns_what_really_happened_and_can_resend(self):
        _, headers = self.issue()
        url = "/internal/agent/v1/jobs/3/send-approval"
        with patch.object(customer_file, "_bridge", thread):
            first = self.customer_client.post(url, headers=headers, json={}).json()
        self.assertEqual(first["state"], "sending")
        self.assertEqual(first["last_text"]["state"], "waiting")
        self.assertTrue(first["sign_link"].endswith("/sign/ro/flow-token"))
        self.set_row(self.rows()[0][0], "sent")
        with patch.object(customer_file, "_bridge", thread):
            again = self.customer_client.post(url, headers=headers, json={}).json()
            forced = self.customer_client.post(url, headers=headers, json={"resend": True}).json()
        self.assertEqual(again["state"], "already")
        self.assertEqual(again["last_text"]["state"], "sent")
        self.assertEqual(forced["state"], "sending")
        self.assertEqual(len(self.rows()), 2)


for _name in [n for n in dir(test_job_flow.JobFlowTests) if n.startswith("test_")]:
    setattr(ApprovalSendTruthTests, _name, None)

if __name__ == "__main__":
    unittest.main()
