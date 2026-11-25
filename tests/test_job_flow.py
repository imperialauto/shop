"""A job from intake to picked up, walked end to end: every status step, approval in person, changes
after approval, the owner's close-out and closing a duplicate. Made-up data, private database."""
import json
import unittest
from unittest.mock import patch

from test_agent_api import ALL, PASSWORD, _Setup

from app import customer_file, ro_status_policy
from app.auth import hash_password
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound
from app.models import Customer, Invoice, LineItem, RepairOrder, User, Vehicle
from app.routes.repair_orders import RO_STATUSES, line_item_revision

SIGNATURE = "data:image/png;base64,iVBORw0KGgo="


def thread(path, phone):
    return {"messages": [{"id": "AC1", "direction": "INBOUND"}]}, None


class JobFlowTests(_Setup):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            db.add(User(id=3, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            c = Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100")
            db.add(c)
            db.add(Vehicle(id=1, customer_id=1, year=2009, make="Chevrolet", model="Impala"))
            db.add(RepairOrder(id=3, customer_id=1, vehicle_id=1, status="intake", public_token="flow-token",
                               concern="Oil leak"))
            db.add_all([
                LineItem(id=1, repair_order_id=3, description="Oil pan gasket", item_type="part", unit_price=80),
                LineItem(id=2, repair_order_id=3, description="Labor: replace oil pan gasket", item_type="labor",
                         quantity=3, unit_price=150),
                LineItem(id=3, repair_order_id=3, description="Oil and filter", item_type="part", unit_price=79.99),
            ])
            db.commit()
        self.owner = self.sign_in("owner")
        self.office = self.sign_in("office")
        self.tech = self.sign_in("tech")
        self.customer_client = __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(
            __import__("app.main", fromlist=["app"]).app, follow_redirects=False)

    # helpers -----------------------------------------------------------------------------------
    def job(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, 3)
            return ro.status, {li.id: li.approval for li in ro.line_items}

    def options(self, role):
        with self.Session() as db:
            ro = db.get(RepairOrder, 3)
            return {k for k, _ in ro_status_policy.manual_status_options(ro, role, RO_STATUSES)}

    def move(self, status, client=None):
        return (client or self.owner).post("/ro/3/update-status", data={"status": status})

    def texts(self):
        with self.Session() as db:
            return db.query(DispatchOutbound).count()

    def edit(self, li_id, **values):
        with self.Session() as db:
            li = db.get(LineItem, li_id)
            data = {"description": li.description, "item_type": li.item_type, "quantity": li.quantity,
                    "unit_price": li.unit_price, "revision": line_item_revision(li)}
        data.update(values)
        return self.owner.post(f"/ro/3/edit-line-item/{li_id}", data=data)

    # tests -------------------------------------------------------------------------------------
    def test_full_walk_intake_to_picked_up_with_link_approval(self):
        self.assertNotIn("delivered", self.options("owner"))
        self.assertNotIn("approval_received", self.options("owner"))
        self.assertEqual(self.move("diagnosing").status_code, 302)
        self.assertIn("asked=3", self.owner.post("/ro/3/request-approval").headers["location"])
        status, lines = self.job()
        self.assertEqual(status, "waiting_approval")
        self.assertEqual(set(lines.values()), {"pending"})
        self.assertNotIn("in_progress", self.options("owner"))
        done = self.customer_client.post("/sign/ro/flow-token", data={
            "approved_by": "Pat", "signature_data": SIGNATURE,
            "line_1": "approved", "line_2": "approved", "line_3": "declined"})
        self.assertLess(done.status_code, 400, done.text[:300])
        status, lines = self.job()
        self.assertEqual(lines, {1: "approved", 2: "approved", 3: "declined"})
        self.assertEqual(status, "approval_received")
        for step in ("waiting_parts", "in_progress", "complete"):
            self.assertIn(step, self.options("owner"), step)
            self.assertEqual(self.move(step).status_code, 302, step)
            self.assertEqual(self.job()[0], step)
        self.assertNotIn("delivered", self.options("tech"))
        self.assertEqual(self.move("delivered", self.tech).status_code, 403)
        self.assertEqual(self.move("delivered").status_code, 302)
        self.assertEqual(self.job()[0], "delivered")
        self.assertNotIn("cancelled", self.options("owner"))
        page = self.owner.get("/ro/3").text
        self.assertIn("Picked up", page)
        self.assertNotIn('id="close-out"', page)

    def test_job3_dead_end_remove_pending_lines_then_approve_in_person(self):
        # What happened on the real job: sent for approval, then lines were taken off.
        self.move("diagnosing")
        self.owner.post("/ro/3/request-approval")
        self.assertEqual(self.owner.post("/ro/3/remove-line/3", data={"confirm": "remove"}).status_code, 303)
        status, lines = self.job()
        self.assertEqual(status, "waiting_approval")  # lines 1 and 2 still wait on the customer
        page = self.owner.get("/ro/3").text
        self.assertIn('id="approve-in-person"', page)
        self.assertIn('id="close-out"', page)
        r = self.owner.post("/ro/3/approve-in-person", data={"source": "in_person", "line_ids": ["1", "2"]})
        self.assertIn("approved_in_person=2", r.headers["location"])
        status, lines = self.job()
        self.assertEqual(status, "approval_received")
        self.assertIn("complete", self.options("owner"))  # the dead end: approval_received could not finish
        self.assertEqual(self.move("complete").status_code, 302)
        self.assertIn("delivered", self.options("owner"))
        self.assertEqual(self.move("delivered").status_code, 302)
        self.assertEqual(self.job()[0], "delivered")
        self.assertEqual(self.texts(), 0)

    def test_removing_every_pending_line_never_leaves_job_waiting(self):
        self.owner.post("/ro/3/request-approval")
        for li in (1, 2, 3):
            self.owner.post(f"/ro/3/remove-line/{li}", data={"confirm": "remove"})
        self.assertEqual(self.job()[0], "diagnosing")
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).filter(DispatchItem.status != "closed").count(), 0)

    def test_close_out_done_and_picked_up_texts_nobody(self):
        self.owner.post("/ro/3/request-approval")
        r = self.owner.post("/ro/3/close-out", data={"line_ids": ["1", "2"], "picked_up": "1",
                                                     "source": "in_person"})
        self.assertEqual(r.headers["location"], "/ro/3?closed_out=delivered")
        status, lines = self.job()
        self.assertEqual(status, "delivered")
        self.assertEqual(lines, {1: "approved", 2: "approved", 3: "declined"})
        with self.Session() as db:
            done = {li.id: bool(li.completed_at) for li in db.get(RepairOrder, 3).line_items}
            self.assertEqual(done, {1: True, 2: True, 3: False})
            self.assertEqual(db.query(Invoice).count(), 0)
            self.assertEqual(db.query(DispatchItem).filter(DispatchItem.status != "closed").count(), 0)
            self.assertTrue(db.query(DispatchAudit).filter(DispatchAudit.action == "job_closed_out").count())
        self.assertEqual(self.texts(), 0)
        self.assertEqual(self.owner.post("/ro/3/close-out", data={"line_ids": ["1"]}).status_code, 409)

    def test_close_out_done_but_not_picked_up_then_picked_up(self):
        r = self.owner.post("/ro/3/close-out", data={"line_ids": ["1", "2", "3"], "source": "phone"})
        self.assertEqual(r.headers["location"], "/ro/3?closed_out=complete")
        self.assertIn("delivered", self.options("owner"))
        self.assertEqual(self.move("delivered").status_code, 302)

    def test_close_out_is_owner_only_and_needs_a_line(self):
        self.assertEqual(self.office.post("/ro/3/close-out", data={"line_ids": ["1"]}).status_code, 403)
        self.assertEqual(self.owner.post("/ro/3/close-out", data={}).status_code, 422)
        self.assertNotIn('id="close-out"', self.office.get("/ro/3").text)

    def test_change_after_approval_only_changed_line_goes_back(self):
        self.owner.post("/ro/3/request-approval")
        self.owner.post("/ro/3/approve-in-person", data={"source": "phone", "line_ids": ["1", "2", "3"]})
        self.move("in_progress")
        r = self.edit(2, quantity=3.5)
        self.assertIn("line_changed=back", r.headers["location"])
        status, lines = self.job()
        self.assertEqual(lines, {1: "approved", 2: "pending", 3: "approved"})
        self.assertEqual(status, "in_progress")  # work on approved lines keeps its column
        with self.Session() as db:
            was = json.loads(db.get(LineItem, 2).changed_from)
            self.assertEqual(was["approval"], "approved")
            self.assertEqual(was["total"], 450)
        page = self.owner.get("/ro/3").text
        self.assertIn("Send changes to Pat for approval", page)
        self.assertIn("Changed, was $450.00", page)
        sign = self.customer_client.get("/sign/ro/flow-token").text
        self.assertIn("<strong>Changed</strong>, was $450.00", sign)
        self.assertEqual(self.texts(), 0)  # editing never texts

        # The owner presses the button: the "we updated the estimate" text, once.
        with patch.object(customer_file, "_bridge", thread):
            first = self.owner.post("/ro/3/text-link-now")
            again = self.owner.post("/ro/3/text-link-now")
        self.assertLess(first.status_code, 400)
        with self.Session() as db:
            sent = db.query(DispatchOutbound).all()
            self.assertEqual(len(sent), 1, again.headers.get("location"))
            body = db.get(DispatchItem, sent[0].item_id).draft_reply
            self.assertIn("we updated the estimate", body)
            self.assertIn("1 item needs your OK", body)

        done = self.customer_client.post("/sign/ro/flow-token", data={
            "approved_by": "Pat", "signature_data": SIGNATURE, "line_2": "approved"})
        self.assertLess(done.status_code, 400, done.text[:300])
        status, lines = self.job()
        self.assertEqual(lines, {1: "approved", 2: "approved", 3: "approved"})
        self.assertNotIn("Send changes to", self.owner.get("/ro/3").text)

    def test_edit_back_restores_the_customer_answer(self):
        self.owner.post("/ro/3/request-approval")
        self.owner.post("/ro/3/approve-in-person", data={"source": "in_person", "line_ids": ["1", "2", "3"]})
        self.edit(1, unit_price=95)
        self.assertEqual(self.job()[1][1], "pending")
        r = self.edit(1, unit_price=80)
        self.assertIn("line_changed=restored", r.headers["location"])
        self.assertEqual(self.job()[1][1], "approved")
        with self.Session() as db:
            li = db.get(LineItem, 1)
            self.assertEqual((li.approval_source, li.changed_from), ("in_person", None))

    def test_removed_approved_line_shows_on_customer_page_until_answered(self):
        self.owner.post("/ro/3/request-approval")
        self.owner.post("/ro/3/approve-in-person", data={"source": "in_person", "line_ids": ["1", "2", "3"]})
        self.owner.post("/ro/3/remove-line/3", data={"confirm": "remove"})
        self.edit(2, unit_price=160)
        sign = self.customer_client.get("/sign/ro/flow-token").text
        self.assertIn("Oil and filter", sign)
        self.customer_client.post("/sign/ro/flow-token", data={
            "approved_by": "Pat", "signature_data": SIGNATURE, "line_2": "approved"})
        with self.Session() as db:
            self.assertIsNone(db.get(RepairOrder, 3).removed_since_approval)

    def test_lines_cannot_change_once_invoiced_or_picked_up(self):
        self.owner.post("/ro/3/approve-in-person", data={"source": "in_person", "line_ids": ["1", "2", "3"]})
        with self.Session() as db:
            db.add(Invoice(repair_order_id=3))
            db.commit()
        self.assertEqual(self.edit(1, unit_price=99).status_code, 409)
        self.assertEqual(self.owner.post("/ro/3/remove-line/1", data={"confirm": "remove"}).status_code, 409)

    def test_parts_ordered_is_offered_once_work_is_approved(self):
        self.assertNotIn("waiting_parts", self.options("owner"))
        self.owner.post("/ro/3/approve-in-person", data={"source": "in_person", "line_ids": ["1", "2"]})
        self.assertIn("waiting_parts", self.options("owner"))
        self.assertEqual(dict(RO_STATUSES)["waiting_parts"], "Parts ordered")
        self.assertEqual(self.move("waiting_parts").status_code, 302)
        self.assertIn("Parts ordered", self.owner.get("/ro/3").text)

    def test_closed_not_going_ahead_is_owner_only_and_leaves_open_lists(self):
        self.assertEqual(self.move("cancelled", self.office).status_code, 403)
        self.assertEqual(self.move("cancelled").status_code, 302)
        self.assertEqual(self.job()[0], "cancelled")
        self.assertFalse(ro_status_policy.is_open(type("R", (), {"status": "cancelled"})()))
        self.assertNotIn("Impala", self.owner.get("/ro/board").text)
        self.assertEqual(self.move("diagnosing", self.office).status_code, 403)  # reopening is the owner's
        self.assertEqual(self.move("diagnosing").status_code, 302)
        with self.Session() as db:
            db.add(Invoice(repair_order_id=3))
            db.commit()
        self.assertEqual(self.move("cancelled").status_code, 409)

    def test_tech_cannot_record_approvals_or_release(self):
        for status in ("waiting_approval", "approval_received", "delivered", "cancelled"):
            self.assertNotIn(status, self.options("tech"), status)
        self.assertEqual(self.tech.post("/ro/3/approve-in-person",
                                        data={"source": "in_person", "line_ids": ["1"]}).status_code, 403)

    def test_assistant_tools_close_out_edit_and_mark_duplicate(self):
        _, headers = self.issue(ALL)
        base = "/internal/agent/v1/jobs/3"
        self.owner.post("/ro/3/approve-in-person", data={"source": "in_person", "line_ids": ["1", "2", "3"]})
        r = self.client.patch(f"{base}/lines/2", headers=headers, json={"quantity": 4})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.job()[1][2], "pending")
        r = self.client.post(f"{base}/close-out", headers=headers,
                             json={"approved_line_ids": [2], "picked_up": False, "source": "phone"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.job()[0], "complete")
        self.assertEqual(self.texts(), 0)
        with self.Session() as db:
            db.add(RepairOrder(id=4, customer_id=1, vehicle_id=1, status="intake", public_token="dup-token"))
            db.commit()
        r = self.client.post("/internal/agent/v1/jobs/4/status", headers=headers, json={"status": "duplicate"})
        self.assertIn(r.status_code, (200, 403), r.text)


if __name__ == "__main__":
    unittest.main()
