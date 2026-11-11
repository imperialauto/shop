"""P0 #120 end to end: owner SMS parts cost -> reviewed quote -> approval link -> customer approval -> invoice.

One synthetic job walks the whole chain and checks, at every customer-facing step, that the owner's
private supplier cost ($81.83) never appears. Made-up data, private in-memory database, no live services:
the owner's YES only returns the text to the (absent) messaging service; nothing is sent.
"""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_dispatch as base
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Invoice, LineItem, PartsOrder, RepairOrder, User
from app.routes import dispatch

OWNER, HEADERS = base.OWNER, base.HEADERS
COST = "81.83"
SIGNATURE = "data:image/png;base64,iVBORw0KGgo="


class SmsEstimateChainTest(unittest.TestCase):
    # Reuse the dispatch suite's private database and owner-text helpers without rerunning its tests.
    setUp = base.QueueWorkflowTests.setUp
    tearDown = base.QueueWorkflowTests.tearDown
    post = base.QueueWorkflowTests.post
    owner_text = base.QueueWorkflowTests.owner_text
    _fusion_sourcing_setup = base.QueueWorkflowTests._fusion_sourcing_setup
    _save_fusion_cost = base.QueueWorkflowTests._save_fusion_cost

    def action(self, body):
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            return self.client.post("/internal/dispatch/owner-action", headers=HEADERS, json=body)

    def customer_texts(self):
        """Every draft or queued text that could reach a customer."""
        with self.Session() as db:
            drafts = [i.draft_reply for i in db.query(DispatchItem).all() if i.draft_reply]
            outbound = [o.body for o in db.query(DispatchOutbound).all() if getattr(o, "body", None)]
        return drafts + outbound

    def assert_cost_private(self, *texts):
        for text in [*texts, *self.customer_texts()]:
            self.assertNotIn(COST, text or "")

    def test_sms_cost_to_reviewed_quote_to_approval_link_to_invoice(self):
        # 1. Customer asks; owner texts what the part cost him. Stored privately on the RO only.
        staged = self._save_fusion_cost(self._fusion_sourcing_setup())
        self.assertEqual(staged.status_code, 200, staged.text)
        self.assertIn("No customer quote or message sent", staged.json()["ownerReply"])
        with self.Session() as db:
            self.assertEqual(db.query(PartsOrder).one().cost, 81.83)
            self.assertEqual(db.query(LineItem).count(), 0)
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
        self.assert_cost_private()

        # 2. Owner sets the customer's price and labor. Lines wait for the customer and are not billable.
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            self.owner_text("o-price", "RO #1: customer part price $155; labor 1 hr shop")
        quote = self.action({"action": "quote_part", "ownerPhone": OWNER, "ownerMessageId": "o-price",
                             "roId": 1, "partPrice": 155, "laborHours": 1, "rateType": "shop",
                             "evidence": "RO #1: customer part price $155; labor 1 hr shop"})
        self.assertEqual(quote.status_code, 200, quote.text)
        with self.Session() as db:
            lines = db.query(LineItem).all()
            self.assertEqual(sorted((li.item_type, li.unit_price) for li in lines), [("labor", 150.0), ("part", 155.0)])
            self.assertTrue(all(li.approval == "pending" and not li.billable for li in lines))
        self.assert_cost_private()

        # 3. Owner confirms fitment and prepares the approval text. It waits for his YES.
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            self.owner_text("o-prep", "prepare approval RO #1 fitment verified")
        prep = self.action({"action": "prepare_link", "ownerPhone": OWNER, "ownerMessageId": "o-prep", "roId": 1,
                            "evidence": "prepare approval RO #1 fitment verified",
                            "customerMessageId": "m-fusion"})  # the customer's real latest text
        self.assertIn("Nothing sent", prep.json()["ownerReply"])
        with self.Session() as db:
            item = db.query(DispatchItem).filter(DispatchItem.category == "approval_link").one()
            item_id = item.id
            token = db.get(RepairOrder, 1).public_token  # the private link is created with the approval text
            self.assertTrue(token)
            self.assertEqual(item.status, "needs_owner")
            self.assertIn(f"/status/{token}", item.draft_reply)
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
        self.assert_cost_private()

        # 4. Owner gets the alert, then YES hands the text to the messaging service exactly once.
        with self.Session() as db, patch.object(dispatch, "OWNER_PHONE", OWNER):
            due = [m for m in dispatch.due_owner_messages(db, base.datetime.now(base.AZ) + base.timedelta(minutes=5))
                   if m["key"].startswith(f"alert:{item_id}:")]
        self.assertEqual(len(due), 1)
        self.assertIn(f"YES {item_id}", due[0]["text"])
        self.client.post("/internal/dispatch/outbound", headers=HEADERS, json={"key": due[0]["key"], "status": "sent"})
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o-yes", f"YES {item_id}")
            again = self.owner_text("o-yes-2", f"YES {item_id}")
        self.assertIn("customerReply", reply, reply)
        sent = reply["customerReply"]["text"]
        self.assertIn(f"/status/{token}", sent)
        self.assertNotIn("customerReply", again)
        self.assert_cost_private(sent)

        # 5. Customer opens the status and approval pages: retail price shown, supplier cost never.
        app.state.read_only = False  # private test database only; tearDown restores the setting
        customer = TestClient(app, follow_redirects=False)
        status_page = customer.get(f"/status/{token}")
        sign_page = customer.get(f"/sign/ro/{token}")
        self.assertEqual((status_page.status_code, sign_page.status_code), (200, 200))
        self.assertIn("155.00", sign_page.text)
        self.assert_cost_private(status_page.text, sign_page.text)

        # 6. Customer approves both lines and signs. Lines become billable; the approval item closes.
        with self.Session() as db:
            ids = [li.id for li in db.query(LineItem).order_by(LineItem.id)]
        done = customer.post(f"/sign/ro/{token}", data={"signature_data": SIGNATURE, "approved_by": "Test Only",
                                                         **{f"line_{i}": "approved" for i in ids}})
        self.assertEqual(done.status_code, 200, done.text)
        with self.Session() as db:
            ro = db.get(RepairOrder, 1)
            self.assertEqual(ro.status, "approval_received")
            self.assertTrue(all(li.approval == "approved" and li.billable for li in ro.line_items))
            self.assertNotEqual(db.get(DispatchItem, item_id).status, "needs_owner")

        # 7. Invoice bills the approved retail lines; parts taxed at 9.2%, labor untaxed; cost stays private.
        with patch("app.routes.invoices.require_user", side_effect=lambda request, db: db.get(User, 1)):
            made = self.client.post("/invoices/create/1", follow_redirects=False)
        self.assertIn(made.status_code, (302, 303), made.text)
        with self.Session() as db:
            invoice = db.query(Invoice).one()
            self.assertEqual((invoice.subtotal, invoice.tax_amount, round(invoice.total, 2)), (305.0, 14.26, 319.26))
            self.assertEqual(db.query(PartsOrder).one().cost, 81.83)
        self.assert_cost_private(customer.get(f"/status/{token}").text)


if __name__ == "__main__":
    unittest.main()
