"""Estimate line edits, estimate totals and the printable estimate PDF. Made-up data only."""
import base64
import json
import secrets
import unittest
from datetime import date, datetime, timezone

from test_agent_api import _Setup
from test_agent_connector import _Flow

from app import estimate_pdf
from app.agent_keys import AgentAction
from app.models import Communication, Customer, Invoice, LineItem, RepairOrder, Vehicle


def make_job(Session, lines, status="intake", invoiced=False):
    with Session() as db:
        c = Customer(first_name="Pat", last_name="Example", phone="(602) 555-0100")
        db.add(c)
        db.flush()
        v = Vehicle(customer_id=c.id, year=2013, make="Ford", model="Explorer", engine="3.5L V6",
                    vin="1FAKE00000V000000")
        db.add(v)
        db.flush()
        ro = RepairOrder(customer_id=c.id, vehicle_id=v.id, status=status, concern="Coolant in oil",
                         public_token=secrets.token_urlsafe(32))
        db.add(ro)
        db.flush()
        ids = []
        for line in lines:
            li = LineItem(repair_order_id=ro.id, **line)
            db.add(li)
            db.flush()
            ids.append(li.id)
        if invoiced:
            db.add(Invoice(repair_order_id=ro.id))
        db.commit()
        return ro.id, ids


SAMPLE = [
    {"description": "Diagnostic findings: oil cooler failed.", "item_type": "misc", "unit_price": 0},
    {"description": "Engine oil cooler", "item_type": "part", "unit_price": 98.99},
    {"description": "Labor: replace engine oil cooler", "item_type": "labor", "quantity": 1.8, "unit_price": 150},
    {"description": "Shop supplies", "item_type": "misc", "unit_price": 27},
    {"description": "OPTIONAL, thermostat: thermostat and gasket", "item_type": "part", "unit_price": 26.65},
    {"description": "OPTIONAL, thermostat: labor", "item_type": "labor", "quantity": 1.4, "unit_price": 150},
]


class TotalsTests(_Setup):
    def test_totals_split_optional_and_skip_declined(self):
        ro_id, ids = make_job(self.Session, SAMPLE + [
            {"description": "Wiper blades", "item_type": "part", "unit_price": 40, "approval": "declined"}])
        with self.Session() as db:
            t = estimate_pdf.totals(db.get(RepairOrder, ro_id))
        rate = estimate_pdf.tax_rate()
        main = t["recommended"]
        self.assertEqual((main["parts"], main["labor"], main["other"], main["labor_hours"]), (98.99, 270.0, 27.0, 1.8))
        self.assertEqual(main["tax"], round(98.99 * rate, 2))
        self.assertEqual(main["total"], round(98.99 + 270 + 27 + round(98.99 * rate, 2), 2))
        self.assertEqual(t["optional"]["total"], round(26.65 + 210 + round(26.65 * rate, 2), 2))
        self.assertEqual(t["total_with_options"], round(main["total"] + t["optional"]["total"], 2))
        self.assertEqual(t["deposit"], round(main["total"] / 2, 2))
        self.assertEqual(t["declined"], 1)


class DiscountTests(_Setup):
    def test_discount_is_money_off_the_recommended_repair_and_not_editable_here(self):
        ro_id, ids = make_job(self.Session, SAMPLE + [
            {"description": "Referral credit", "item_type": "discount", "unit_price": -25, "taxable": False,
             "approval": "approved"}])
        with self.Session() as db:
            ro = db.get(RepairOrder, ro_id)
            t = estimate_pdf.totals(ro)
            self.assertTrue(estimate_pdf.build(ro).startswith(b"%PDF"))
        main = t["recommended"]
        self.assertEqual((main["discount"], main["other"]), (-25.0, 27.0))
        self.assertEqual(main["tax"], round(98.99 * estimate_pdf.tax_rate(), 2))
        self.assertEqual(main["total"], round(98.99 + 270 + 27 - 25 + main["tax"], 2))
        self.assertEqual(t["optional"]["discount"], 0)
        _, headers = self.issue()
        for call in (lambda: self.client.patch(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[-1]}", headers=headers,
                                               json={"unit_price": 5}),
                     lambda: self.client.delete(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[-1]}", headers=headers)):
            out = call()
            self.assertEqual(out.status_code, 409)
            self.assertIn("discount", out.json()["detail"])


class LineEditTests(_Setup):
    def test_edit_and_remove_draft_lines_recalculate_and_log(self):
        _, headers = self.issue()
        ro_id, ids = make_job(self.Session, SAMPLE)
        out = self.client.patch(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[1]}", headers=headers,
                                json={"unit_price": 108.5, "description": "Engine oil cooler, Dorman 918-970"})
        self.assertEqual(out.status_code, 200, out.text)
        body = out.json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["line"]["unit_price"], 108.5)
        self.assertEqual(body["totals"]["recommended"]["parts"], 108.5)

        same = self.client.patch(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[1]}", headers=headers,
                                 json={"unit_price": 108.5})
        self.assertFalse(same.json()["changed"])
        empty = self.client.patch(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[1]}", headers=headers, json={})
        self.assertEqual(empty.status_code, 422)

        gone = self.client.delete(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[3]}", headers=headers)
        self.assertEqual(gone.status_code, 200, gone.text)
        self.assertEqual(gone.json()["removed"]["description"], "Shop supplies")
        self.assertEqual(gone.json()["totals"]["recommended"]["other"], 0)
        self.assertNotIn(ids[3], [li["id"] for li in gone.json()["lines"]])

        with self.Session() as db:
            actions = [(a.action, a.target_id) for a in db.query(AgentAction).order_by(AgentAction.id)]
            self.assertEqual(actions, [("estimate.edit_line", ro_id), ("estimate.remove_line", ro_id)])
            self.assertIn("98.99", db.query(AgentAction).first().summary)
            self.assertEqual(db.query(Communication).count(), 0)

    def test_lines_the_customer_was_asked_about_are_refused(self):
        _, headers = self.issue()
        ro_id, ids = make_job(self.Session, [
            {"description": "Pending", "unit_price": 10, "approval": "pending"},
            {"description": "Approved", "unit_price": 10, "approval": "approved"}])
        for li_id in ids:
            self.assertEqual(self.client.patch(f"/internal/agent/v1/jobs/{ro_id}/lines/{li_id}", headers=headers,
                                               json={"unit_price": 1}).status_code, 409)
            self.assertEqual(self.client.delete(f"/internal/agent/v1/jobs/{ro_id}/lines/{li_id}",
                                                headers=headers).status_code, 409)
        with self.Session() as db:
            self.assertEqual([li.unit_price for li in db.query(LineItem).order_by(LineItem.id)], [10, 10])
            self.assertEqual(db.query(AgentAction).count(), 0)

    def test_invoiced_finished_other_job_and_done_lines_are_refused(self):
        _, headers = self.issue()
        inv_id, inv_lines = make_job(self.Session, [{"description": "A", "unit_price": 5}], invoiced=True)
        done_id, done_lines = make_job(self.Session, [{"description": "B", "unit_price": 5}], status="complete")
        open_id, open_lines = make_job(self.Session, [{"description": "C", "unit_price": 5}])
        self.assertEqual(self.client.patch(f"/internal/agent/v1/jobs/{inv_id}/lines/{inv_lines[0]}", headers=headers,
                                           json={"unit_price": 1}).status_code, 409)
        self.assertEqual(self.client.patch(f"/internal/agent/v1/jobs/{done_id}/lines/{done_lines[0]}",
                                           headers=headers, json={"unit_price": 1}).status_code, 409)
        # a line id from another job
        self.assertEqual(self.client.patch(f"/internal/agent/v1/jobs/{open_id}/lines/{inv_lines[0]}",
                                           headers=headers, json={"unit_price": 1}).status_code, 404)
        with self.Session() as db:
            db.get(LineItem, open_lines[0]).completed_at = datetime.now(timezone.utc)
            db.commit()
        self.assertEqual(self.client.delete(f"/internal/agent/v1/jobs/{open_id}/lines/{open_lines[0]}",
                                            headers=headers).status_code, 409)

    def test_read_only_key_cannot_edit(self):
        _, headers = self.issue(scopes=["estimates:read"])
        ro_id, ids = make_job(self.Session, SAMPLE)
        self.assertEqual(self.client.patch(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[1]}", headers=headers,
                                           json={"unit_price": 1}).status_code, 403)
        self.assertEqual(self.client.delete(f"/internal/agent/v1/jobs/{ro_id}/lines/{ids[1]}",
                                            headers=headers).status_code, 403)


class PdfTests(_Setup):
    def test_pdf_builds_with_all_sections(self):
        ro_id, _ = make_job(self.Session, SAMPLE + [
            {"description": "Wiper blades", "item_type": "part", "unit_price": 40, "approval": "declined"},
            {"description": "Long " * 400, "item_type": "labor", "unit_price": 150}])
        with self.Session() as db:
            pdf = estimate_pdf.build(db.get(RepairOrder, ro_id), today=date(2026, 11, 23))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 2000)

    def test_empty_job_still_prints(self):
        ro_id, _ = make_job(self.Session, [])
        with self.Session() as db:
            self.assertTrue(estimate_pdf.build(db.get(RepairOrder, ro_id)).startswith(b"%PDF"))

    def test_staff_print_link_needs_office_login(self):
        ro_id, _ = make_job(self.Session, SAMPLE)
        self.assertEqual(self.client.get(f"/ro/{ro_id}/estimate.pdf").status_code, 302)
        got = self.sign_in("office").get(f"/ro/{ro_id}/estimate.pdf")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.headers["content-type"], "application/pdf")
        self.assertIn(f"Estimate_RO{ro_id}_Example.pdf", got.headers["content-disposition"])
        page = self.sign_in("owner").get(f"/ro/{ro_id}")
        self.assertIn(f"/ro/{ro_id}/estimate.pdf", page.text)

    def test_agent_pdf_needs_read_scope(self):
        ro_id, _ = make_job(self.Session, SAMPLE)
        _, headers = self.issue(scopes=["customers:read"])
        self.assertEqual(self.client.get(f"/internal/agent/v1/jobs/{ro_id}/estimate.pdf", headers=headers).status_code, 403)
        _, headers = self.issue(scopes=["estimates:read"])
        got = self.client.get(f"/internal/agent/v1/jobs/{ro_id}/estimate.pdf", headers=headers)
        self.assertEqual(got.status_code, 200)
        self.assertTrue(got.content.startswith(b"%PDF"))


class ConnectorToolTests(_Flow):
    def test_tools_listed_edit_and_pdf(self):
        headers, _ = self.connect()
        names = [t["name"] for t in self.rpc(headers, "tools/list").json()["result"]["tools"]]
        for name in ("edit_estimate_line", "remove_estimate_line", "get_estimate_pdf"):
            self.assertIn(name, names)
        ro_id, ids = make_job(self.Session, SAMPLE)
        edited = self.rpc(headers, "tools/call", {"name": "edit_estimate_line", "arguments": {
            "job_id": ro_id, "line_id": ids[2], "quantity": 2.0}}).json()["result"]
        self.assertFalse(edited["isError"], edited)
        self.assertEqual(json.loads(edited["content"][0]["text"])["totals"]["recommended"]["labor"], 300.0)
        removed = self.rpc(headers, "tools/call", {"name": "remove_estimate_line", "arguments": {
            "job_id": ro_id, "line_id": ids[4]}}).json()["result"]
        self.assertFalse(removed["isError"], removed)
        pdf = self.rpc(headers, "tools/call", {"name": "get_estimate_pdf", "arguments": {"job_id": ro_id}}).json()["result"]
        self.assertFalse(pdf["isError"], pdf)
        info = json.loads(pdf["content"][0]["text"])
        self.assertTrue(info["print_link"].endswith(f"/ro/{ro_id}/estimate.pdf"))
        blob = pdf["content"][1]["resource"]
        self.assertEqual(blob["mimeType"], "application/pdf")
        self.assertTrue(base64.b64decode(blob["blob"]).startswith(b"%PDF"))
        missing = self.rpc(headers, "tools/call", {"name": "get_estimate_pdf", "arguments": {"job_id": 999}}).json()["result"]
        self.assertTrue(missing["isError"])


if __name__ == "__main__":
    unittest.main()
