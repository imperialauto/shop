"""U-Haul requests in from email, the jobs they open, the bill-out sheet and payments. Made-up data only.

Nothing in this flow acts at U-Haul, texts, calls or emails anyone: the tests check that too."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_agent_api import PASSWORD, _Setup

from app import uhaul
from app.auth import hash_password
from app.models import Customer, LineItem, PartsOrder, RepairOrder, User, Vehicle
from app.uhaul import UhaulPayment, UhaulRequest

FIX = Path(__file__).parent / "fixtures" / "uhaul"
KEY = "k" * 32
ROADSIDE_SUBJECT = "U-Haul Customer Pat Example needs roadside assistance!"
ROADSIDE_BODY = "Can you help? Click here for details. https://roadside.uhaul.net/r/made-up-token"


def mail(n, **extra):
    data = {"message_id": f"msg-{n}", "thread_id": f"thread-{n}", "from": "U-Haul <noreply@roadside.uhaul.com>",
            "subject": ROADSIDE_SUBJECT, "date": datetime.now(timezone.utc).isoformat(), "body": ROADSIDE_BODY, "links": []}
    data.update(extra)
    return data


def rpo_mail(n, fixture, subject, sender="Repair Dispatch <721_rdm@uhaul.com>"):
    return mail(n, subject=subject, body=(FIX / fixture).read_text(), **{"from": sender})


class ParseTests(unittest.TestCase):
    def test_reads_the_three_repair_po_layouts(self):
        a = uhaul.parse("RPO Number: 478-00999XX Equip: FS9999A", (FIX / "rpo_self_dispatch.txt").read_text())
        self.assertEqual((a["rpo_number"], a["equipment_number"], a["year_make"]), ("478-00999XX", "3159 FS 9999A", "2008 / UHI"))
        self.assertIn("Back door not closing", a["reason"])
        self.assertEqual(a["rdm_email"], "858_rdm@uhaul.com")
        b = uhaul.parse("U-Haul Repair 5108 DC 9999X RPO: 478-00888BA", (FIX / "rpo_dispatch.txt").read_text())
        self.assertEqual((b["rpo_number"], b["estimated_amount"], b["vin"]), ("478-00888BA", 150.0, "1FDXE4FS5JDC99999"))
        self.assertTrue(b["reason"].startswith("AC blowing hot air"))
        self.assertEqual(b["repair_by"].isoformat(), "2026-06-19")
        c = uhaul.parse("U-Haul PO 478-007777Z Needs Review", (FIX / "rpo_needs_review.txt").read_text())
        self.assertEqual((c["rpo_number"], c["hotline_ref"], c["equipment_number"]), ("478-007777Z", "3000001", "JH0000C"))
        self.assertIn("voided on 6/25", c["review_reason"])
        self.assertEqual(c["cause_text"], "Transmission Issues")

    def test_roadside_offer_keeps_only_a_uhaul_https_link(self):
        r = uhaul.parse(ROADSIDE_SUBJECT, ROADSIDE_BODY)
        self.assertEqual((r["kind"], r["customer_name"], r["offer_url"]),
                         ("roadside", "Pat Example", "https://roadside.uhaul.net/r/made-up-token"))
        for body in ("https://evil.example/roadside.uhaul.net", "http://roadside.uhaul.net/r/x",
                     "https://uhaul.net.evil.example/r", "https://user@roadside.uhaul.net/r", "no link at all"):
            self.assertEqual(uhaul.parse(ROADSIDE_SUBJECT, body)["offer_url"], "", body)

    def test_other_uhaul_mail_is_not_a_request(self):
        self.assertIsNone(uhaul.parse("Please upload COI to Vendor Portal", "Your certificate has expired."))

    def test_only_uhaul_domains_count_as_uhaul(self):
        self.assertTrue(uhaul.from_uhaul("noreply@roadside.uhaul.com"))
        self.assertTrue(uhaul.from_uhaul("Kelly <Kelly_Fees@uhaul.com>"))
        for sender in ("x@uhaul.com.evil.example", "x@notuhaul.com", "uhaul.com <x@evil.example>", ""):
            self.assertFalse(uhaul.from_uhaul(sender), sender)


class FlowTests(_Setup):
    def setUp(self):
        super().setUp()
        self.uenv = patch.dict(os.environ, {"UHAUL_ENABLED": "true", "UHAUL_INTAKE_KEY": KEY})
        self.uenv.start()
        uhaul._ready.clear()
        with self.Session() as db:
            db.add(User(id=3, username="tech", password_hash=hash_password(PASSWORD), role="tech"))
            db.commit()
        self.owner = self.sign_in("owner")

    def tearDown(self):
        self.uenv.stop()
        uhaul._ready.clear()
        super().tearDown()

    def post(self, payload, key=KEY):
        return self.client.post("/webhooks/uhaul", content=json.dumps(payload),
                                headers={"X-Uhaul-Intake-Key": key, "Content-Type": "application/json"})

    def rows(self):
        with self.Session() as db:
            return db.query(UhaulRequest).order_by(UhaulRequest.id).all()

    # ------------------------------------------------ intake

    def test_webhook_needs_the_key_and_is_off_without_one(self):
        self.assertEqual(self.post(mail(1), key="wrong").status_code, 401)
        self.assertEqual(self.post(mail(1), key="").status_code, 401)
        with patch.dict(os.environ, {"UHAUL_INTAKE_KEY": ""}):
            self.assertEqual(self.post(mail(1)).status_code, 404)
        with patch.dict(os.environ, {"UHAUL_INTAKE_KEY": "short"}):
            self.assertEqual(self.post(mail(1), key="short").status_code, 401)  # a weak key never opens it
        self.assertEqual(self.rows(), [])

    def test_resends_and_repeats_make_one_card(self):
        self.assertEqual(self.post(mail(1)).json()["outcome"], "new")
        self.assertEqual(self.post(mail(1)).json()["outcome"], "duplicate")
        two_hours = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        self.assertEqual(self.post(mail(2, thread_id="thread-1", date=two_hours)).json()["outcome"], "resend")
        self.assertEqual(self.post(mail(3, thread_id="other-thread")).json()["outcome"], "resend")  # same name, same day
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].resend_count, rows[0].status), (2, "new"))

    def test_mail_not_from_uhaul_or_not_a_request_is_ignored(self):
        self.assertEqual(self.post(mail(1, **{"from": "U-Haul <noreply@uhaul.com.evil.example>"})).json()["outcome"], "ignored")
        self.assertEqual(self.post(mail(2, subject="Please upload COI", body="Expired")).json()["outcome"], "ignored")
        self.assertEqual(self.client.post("/webhooks/uhaul", content=b"not json",
                                          headers={"X-Uhaul-Intake-Key": KEY}).status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_needs_review_resend_lands_on_its_po(self):
        self.post(rpo_mail(1, "rpo_dispatch.txt", "U-Haul Repair 5108 DC 9999X RPO: 478-00888BA"))
        body = (FIX / "rpo_needs_review.txt").read_text().replace("478-007777Z", "478-00888BA")
        out = self.post(mail(2, subject="U-Haul PO 478-00888BA Needs Review", body=body, **{"from": "kelly_fees@uhaul.com"}))
        self.assertEqual(out.json()["outcome"], "resend")
        [row] = self.rows()
        self.assertIn("voided on 6/25", row.review_reason)
        self.assertEqual(row.equipment_number, "5108 DC 9999X")  # what it already knew stays

    def test_not_switched_on_until_the_setting_and_tables(self):
        with patch.dict(os.environ, {"UHAUL_ENABLED": "false"}):
            uhaul._ready.clear()
            self.assertEqual(self.post(mail(1)).status_code, 503)
            self.assertIn("isn't switched on yet", self.owner.get("/uhaul").text)
            self.assertNotIn('href="/uhaul"', self.owner.get("/uhaul").text)
            self.assertNotIn("uhaul-home", self.owner.get("/dashboard").text)

    # ------------------------------------------------ alert and answering

    def test_new_offer_shows_on_home_and_reads_missed_after_15_minutes(self):
        self.post(mail(1))
        home = self.owner.get("/dashboard").text
        self.assertIn("uhaul-home", home)
        self.assertIn("Pat Example", home)
        self.assertIn('href="https://roadside.uhaul.net/r/made-up-token" target="_blank" rel="noopener noreferrer"', home)
        [row] = self.rows()
        later = datetime.now(timezone.utc) + timedelta(minutes=16)
        self.assertEqual(uhaul.shown_status(row, later), "missed")
        with self.Session() as db:
            self.assertEqual(uhaul.board(db, later)["missed_month"], 1)

    def test_taking_it_opens_one_uhaul_fleet_job(self):
        self.post(mail(1))
        self.post(rpo_mail(2, "rpo_dispatch.txt", "U-Haul Repair 5108 DC 9999X RPO: 478-00888BA"))
        a, b = self.rows()
        self.assertEqual(self.owner.post(f"/uhaul/{a.id}/take").status_code, 303)
        self.assertEqual(self.owner.post(f"/uhaul/{b.id}/take").status_code, 303)
        self.assertEqual(self.owner.post(f"/uhaul/{a.id}/take").status_code, 409)  # only once
        with self.Session() as db:
            fleets = db.query(Customer).filter(Customer.customer_type == "fleet").all()
            self.assertEqual([c.fleet_name for c in fleets], ["U-Haul"])
            ro = db.get(RepairOrder, db.get(UhaulRequest, b.id).repair_order_id)
            self.assertIn("U-Haul repair PO 478-00888BA", ro.concern)
            self.assertEqual((ro.vehicle.year, ro.vehicle.make, ro.vehicle.model, ro.vehicle.vin),
                             (2018, "Ford", "E450", "1FDXE4FS5JDC99999"))
            self.assertEqual(db.get(UhaulRequest, a.id).status, "accepted")

    def test_passing_and_tech_limits(self):
        self.post(mail(1))
        [row] = self.rows()
        tech = self.sign_in("tech")
        self.assertEqual(tech.post(f"/uhaul/{row.id}/take").status_code, 403)
        self.assertEqual(tech.get("/uhaul").status_code, 200)
        self.assertNotIn("I took it", tech.get("/uhaul").text)
        self.assertEqual(self.owner.post(f"/uhaul/{row.id}/pass").status_code, 303)
        self.assertEqual(self.rows()[0].status, "declined")
        self.assertEqual(self.owner.post(f"/uhaul/{row.id}/take").status_code, 409)

    def test_office_login_can_work_it(self):
        self.post(mail(1))
        [row] = self.rows()
        office = self.sign_in("office")
        self.assertEqual(office.get("/uhaul").status_code, 200)
        self.assertEqual(office.post(f"/uhaul/{row.id}/take").status_code, 303)
        self.assertEqual(office.get(f"/uhaul/{row.id}").status_code, 200)

    def test_add_by_hand(self):
        r = self.owner.post("/uhaul/new", data={"kind": "roadside", "customer_name": "Sam Example", "hotline_ref": "3000002"})
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.rows()[0].customer_name, "Sam Example")
        bad = self.owner.post("/uhaul/new", data={"kind": "rpo", "rpo_number": "nope"})
        self.assertIn("error=", bad.headers["location"])

    # ------------------------------------------------ the whole roadside job, to paid

    def _start(self):
        self.post(mail(1))
        [row] = self.rows()
        self.owner.post(f"/uhaul/{row.id}/take")
        return row.id

    def test_steps_go_forward_in_order_and_undo(self):
        rid = self._start()
        tech = self.sign_in("tech")
        self.assertEqual(tech.post(f"/uhaul/{rid}/step", data={"to": "done"}).status_code, 409)
        for to in ("en_route", "on_site", "done", "back"):
            self.assertEqual(tech.post(f"/uhaul/{rid}/step", data={"to": to}).status_code, 303, to)
        row = self.rows()[0]
        self.assertTrue(all((row.en_route_at, row.on_site_at, row.done_at, row.back_at)))
        tech.post(f"/uhaul/{rid}/step", data={"to": "undo"})
        row = self.rows()[0]
        self.assertEqual((row.status, row.back_at), ("done", None))

    def test_bill_out_sheet_flags_then_ready_submitted_paid(self):
        rid = self._start()
        t0 = datetime(2026, 11, 27, 17, 0, tzinfo=timezone.utc)
        with self.Session() as db:
            req = db.get(UhaulRequest, rid)
            req.status, req.en_route_at, req.on_site_at = "back", t0, t0 + timedelta(minutes=30)
            req.done_at, req.back_at = t0 + timedelta(minutes=75), t0 + timedelta(minutes=105)
            db.add_all([
                LineItem(repair_order_id=req.repair_order_id, description="Jump start and battery test", item_type="labor",
                         quantity=0.5, unit_price=150, approval="approved"),
                LineItem(repair_order_id=req.repair_order_id, description="Battery 24F", item_type="part",
                         quantity=1, unit_price=210, approval="approved"),
                LineItem(repair_order_id=req.repair_order_id, description="Wipers", item_type="part",
                         quantity=1, unit_price=30, approval="declined"),
            ])
            db.commit()
            s = uhaul.sheet(db, db.get(UhaulRequest, rid))
        self.assertEqual(s["rows"][0]["code"], "91")
        self.assertEqual(s["rows"][0]["qty"], 1.0)  # 30 min out + 30 min back, port to port
        self.assertEqual(s["rows"][1]["code"], "Group 10")
        self.assertEqual([r["what"] for r in s["rows"]], ["Emergency Aid Travel Time (port to port)",
                                                          "Jump start and battery test", "Battery 24F"])
        self.assertTrue(s["blocked"])
        text = " ".join(f["text"] for f in s["flags"])
        for expected in ("No RPO number yet", "Odometer missing", "Correction is empty", "Part number missing"):
            self.assertIn(expected, text)
        self.assertEqual(self.owner.post(f"/uhaul/{rid}/ready").status_code, 409)

        with self.Session() as db:
            ro_id = db.get(UhaulRequest, rid).repair_order_id
            db.add(PartsOrder(repair_order_id=ro_id, vendor="AutoZone", part_number="24F-DL", description="Battery 24F", cost=150))
            db.commit()
        page = self.owner.get(f"/uhaul/{rid}").text
        self.assertIn("First: Request RPO in the portal", page)
        saved = self.owner.post(f"/uhaul/{rid}/billing", data={
            "rpo_number": "478-00555ab", "hotline_ref": "3000003", "equipment_number": "TM0000B", "odometer": "48210",
            "travel_basis": "hours", "condition": "No start", "cause": "Dead battery", "correction": "Jumped and replaced battery"})
        self.assertEqual(saved.status_code, 303)
        with self.Session() as db:
            s = uhaul.sheet(db, db.get(UhaulRequest, rid))
        # The receipt check only bites once parts receipts are switched on; here only the cost and part # count.
        blocking = [f["text"] for f in s["flags"] if f["block"]]
        self.assertEqual([b for b in blocking if "receipt" not in b], [])
        if not blocking:
            self.assertEqual(self.owner.post(f"/uhaul/{rid}/ready").status_code, 303)
        else:
            with self.Session() as db:
                db.get(UhaulRequest, rid).status = "ready"
                db.commit()
        self.assertEqual(self.owner.post(f"/uhaul/{rid}/submitted", data={"invoice": "1040", "amount": ""}).status_code, 409)
        self.assertEqual(self.owner.post(f"/uhaul/{rid}/submitted", data={"invoice": "1040", "amount": "300"}).status_code, 303)
        self.assertEqual(self.rows()[0].status, "submitted")
        self.assertIn("$300.00 due", self.owner.get("/uhaul").text)
        # short pay first, then the rest
        self.owner.post("/uhaul/payments", data={"rpo_number": "478-00555AB", "amount": "250", "paid_on": "2026-12-04", "reference": "CHK1"})
        row = self.rows()[0]
        self.assertEqual(row.status, "submitted")
        self.assertIn("$50.00 less than billed", self.owner.get(f"/uhaul/{rid}").text)
        dup = self.owner.post("/uhaul/payments", data={"rpo_number": "478-00555AB", "amount": "250", "reference": "CHK1"})
        self.assertIn("already+recorded", dup.headers["location"].replace("%20", "+"))
        self.owner.post("/uhaul/payments", data={"rpo_number": "478-00555AB", "amount": "50", "reference": "CHK2"})
        self.assertEqual(self.rows()[0].status, "paid")

    def test_miles_instead_of_hours_and_time_checks(self):
        rid = self._start()
        for to in ("en_route", "on_site", "done", "back"):
            self.owner.post(f"/uhaul/{rid}/step", data={"to": to})
        r = self.owner.post(f"/uhaul/{rid}/billing", data={"travel_basis": "miles", "travel_miles": "42", "odometer": "1",
                                                          "on_site_at": "2026-11-27T09:00", "en_route_at": "2026-11-27T10:00",
                                                          "done_at": "2026-11-27T11:00", "back_at": "2026-11-27T12:00"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("out of order", r.text)
        r = self.owner.post(f"/uhaul/{rid}/billing", data={"travel_basis": "miles", "travel_miles": "42", "odometer": "1",
                                                          "done_at": ""})
        self.assertEqual(r.status_code, 422)
        self.owner.post(f"/uhaul/{rid}/billing", data={"travel_basis": "miles", "travel_miles": "42", "odometer": "1"})
        with self.Session() as db:
            s = uhaul.sheet(db, db.get(UhaulRequest, rid))
        self.assertEqual((s["rows"][0]["qty"], s["rows"][0]["unit"]), (42.0, "miles"))
        self.assertEqual(sum(1 for row in s["rows"] if row["code"] == "91"), 1)  # never both

    def test_shop_repair_po_has_no_travel_line(self):
        self.post(rpo_mail(1, "rpo_dispatch.txt", "U-Haul Repair 5108 DC 9999X RPO: 478-00888BA"))
        [row] = self.rows()
        self.owner.post(f"/uhaul/{row.id}/take")
        self.owner.post(f"/uhaul/{row.id}/step", data={"to": "shop"})
        self.owner.post(f"/uhaul/{row.id}/step", data={"to": "done"})
        with self.Session() as db:
            s = uhaul.sheet(db, db.get(UhaulRequest, row.id))
        self.assertFalse(any(r["code"] == "91" for r in s["rows"]))
        self.assertFalse(any("No leave time" in f["text"] for f in s["flags"]))

    def test_job_over_uhaul_estimate_is_flagged(self):
        self.post(rpo_mail(1, "rpo_dispatch.txt", "U-Haul Repair 5108 DC 9999X RPO: 478-00888BA"))
        [row] = self.rows()
        self.owner.post(f"/uhaul/{row.id}/take")
        with self.Session() as db:
            req = db.get(UhaulRequest, row.id)
            db.add(LineItem(repair_order_id=req.repair_order_id, description="A/C diag and recharge", item_type="labor",
                            quantity=2, unit_price=150, approval="approved"))
            db.commit()
            s = uhaul.sheet(db, db.get(UhaulRequest, row.id))
        self.assertTrue(any("over U-Haul's $150.00 estimate" in f["text"] for f in s["flags"]))

    def test_nothing_is_sent_anywhere(self):
        with patch("httpx.AsyncHTTPTransport.handle_async_request") as http_post, \
                patch("httpx.HTTPTransport.handle_request") as sync_post:
            rid = self._start()
            for to in ("en_route", "on_site", "done", "back"):
                self.owner.post(f"/uhaul/{rid}/step", data={"to": to})
            self.owner.get(f"/uhaul/{rid}")
            self.owner.get("/uhaul")
        http_post.assert_not_called()
        sync_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
