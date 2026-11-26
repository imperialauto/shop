"""The new job-page pieces used together, plus every main screen opened as owner, office and tech.
Made-up data, private database. Nothing is texted (the send path is checked by counting outbound rows)."""
from datetime import date

import test_job_flow

from app.dispatch_model import DispatchAudit, DispatchOutbound
from app.models import LineItem, PartsOrder, RepairOrder


class FeaturesTogetherTests(test_job_flow.JobFlowTests):
    def test_package_then_approval_then_parts_arrive_then_clock_then_done(self):
        # 1. A canned package adds normal lines to the estimate.
        r = self.owner.post("/ro/3/add-package", data={"package": "oil-change"})
        self.assertEqual(r.status_code, 303)
        with self.Session() as db:
            added = [li for li in db.get(RepairOrder, 3).line_items if li.id not in (1, 2, 3)]
        self.assertTrue(added)
        self.assertTrue(all(li.approval is None for li in added))

        # 2. Ask, and the customer approves everything on their page.
        self.move("diagnosing")
        self.owner.post("/ro/3/request-approval")
        self.assertEqual(self.job()[0], "waiting_approval")
        with self.Session() as db:
            for li in db.get(RepairOrder, 3).line_items:
                li.approval = "approved"
            db.get(RepairOrder, 3).status = "approval_received"
            db.commit()

        # 3. Parts: order one, hold the job for it, mark it in. The job moves on by itself.
        with self.Session() as db:
            db.add(PartsOrder(id=1, repair_order_id=3, description="Oil pan gasket", status="ordered",
                              eta=date(2026, 12, 1)))
            db.commit()
        self.assertIn(self.owner.post("/ro/3/parts/hold").status_code, (302, 303))
        self.assertEqual(self.job()[0], "waiting_parts")
        page = self.owner.get("/ro/3").text
        self.assertIn("Oil pan gasket", page)
        r = self.tech.post("/ro/3/parts/1/arrived")  # techs can mark parts in
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.job()[0], "in_progress")
        with self.Session() as db:
            self.assertTrue(db.query(DispatchAudit).filter(DispatchAudit.action == "parts_in_job_moved").count())

        # 4. The tech clocks on and off; the job page shows clocked against billed.
        self.assertEqual(self.tech.post("/ro/3/clock", data={"operation": "start"}).status_code, 303)
        self.assertEqual(self.tech.post("/ro/3/clock", data={"operation": "stop"}).status_code, 303)
        self.assertIn("job-timer", self.owner.get("/ro/3").text)

        # 5. Done and picked up from the owner's close-out: no text to the customer.
        before = self.texts()
        with self.Session() as db:
            ids = [li.id for li in db.get(RepairOrder, 3).line_items if li.item_type != "discount"]
        r = self.owner.post("/ro/3/close-out", data={"line_ids": ids, "picked_up": "1", "source": "in_person"})
        self.assertIn(r.status_code, (302, 303))
        self.assertEqual(self.job()[0], "delivered")
        self.assertEqual(self.texts(), before)

    def test_every_main_screen_opens_for_each_role(self):
        everyone = ["/dashboard", "/ro/3", "/ro/board", "/appointments/calendar", "/my-day", "/board"]
        office = ["/invoices/", "/dashboard/inbox", "/customers/1"]
        owner = ["/finance/", "/settings/canned-jobs", "/settings"]
        for path in everyone:
            for name, client in (("owner", self.owner), ("office", self.office), ("tech", self.tech)):
                r = client.get(path)
                self.assertLess(r.status_code, 400, f"{name} {path}: {r.status_code}")
        for path in office:
            for name, client in (("owner", self.owner), ("office", self.office)):
                self.assertLess(client.get(path).status_code, 400, f"{name} {path}")
            self.assertGreaterEqual(self.tech.get(path).status_code, 300, f"tech {path}")
        for path in owner:
            self.assertLess(self.owner.get(path).status_code, 400, f"owner {path}")
        # The customer's approval page opens without signing in.
        self.assertEqual(self.customer_client.get("/status/flow-token").status_code, 200)

    def test_tech_job_page_hides_money_tools(self):
        page = self.tech.get("/ro/3").text
        self.assertNotIn("Send changes for approval", page)
        self.assertNotIn("/close-out", page)


# Keep the parent's tests from running twice.
for _name in [n for n in dir(test_job_flow.JobFlowTests) if n.startswith("test_")]:
    setattr(FeaturesTogetherTests, _name, None)
