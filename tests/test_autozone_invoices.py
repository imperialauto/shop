"""AutoZone invoice PDFs read line by line, held against the job's part lines, and matched to jobs by PO, with a
not-matched-yet list. Made-up data only (tests/autozone_fixture.py)."""
import json
import unittest

from sqlalchemy import text

import autozone_fixture as az
from test_po_receipts import JPEG, JobPoAndReceiptTests, receipt_pdf

from app import parts_receipts, store_invoices
from app.models import LineItem
from app.parts_receipts import PartsReceipt

PDF = "application/pdf"


class ReadingTests(unittest.TestCase):
    def test_reads_the_header_lines_totals_and_open_cores(self):
        inv = store_invoices.parse(az.pdf(), PDF)
        self.assertEqual((inv["kind"], inv["store"], inv["store_city"], inv["store_state"]),
                         ("autozone", "AutoZone 09999", "Phoenix, AZ 85000", "AZ"))
        self.assertEqual((inv["invoice_number"], inv["po"], inv["order_date"]),
                         ("09999000123", "IAC-PO-0021", "11/25/2026 01:07 PM"))
        self.assertEqual((inv["subtotal"], inv["tax"], inv["total"]), (868.64, 0.0, 868.64))
        items = inv["items"]
        self.assertEqual([i["parts"][0] for i in items], ["TS34-31351B", "MS34-31422B", "SZND536G", "82180", "9-0738SWP", "78DT"])
        self.assertEqual(round(sum(i["total"] for i in items), 2), inv["subtotal"])
        shocks = items[1]
        self.assertEqual((shocks["qty"], shocks["list"], shocks["cost"], shocks["core"], shocks["total"], shocks["sku"]),
                         (2, 69.86, 39.33, 0.0, 78.66, "000087143"))
        self.assertEqual(shocks["name"], "MONOTUBE S Duralast Monotube Shock")
        oil = items[2]
        self.assertEqual(oil["parts"], ["SZND536G", "SZND536GND"])  # a second part number on the next row
        self.assertEqual(oil["deal"], "STP FS Box You saved $50.00")
        self.assertEqual(items[3]["name"], "ULTRA BLACK GAS Permatex Ultra Black RTV Silicone Gasket Maker")
        self.assertEqual(items[3]["sku"], "000554295")
        self.assertEqual(items[5]["core"], 22.0)
        self.assertEqual(inv["cores_open_total"], 44.0)
        self.assertEqual([(c["part"], c["date"], c["amount"]) for c in inv["cores_open"]],
                         [("GC2", "2026-09-25", 22.0), ("78-T", "2026-10-08", 22.0)])

    def test_never_keeps_how_it_was_paid(self):
        saved = json.dumps(store_invoices.parse(az.pdf(), PDF))
        for word in ("VISA", "****", "000000", "Customer #", "10000001"):
            self.assertNotIn(word, saved)

    def test_parts_over_two_pages(self):
        first = [c for c in az.HEADER] + az.ITEMS[:10]
        second = [(71, 605, "Part #"), (118, 605, "QTY"), (234, 605, "Description"), (393, 605, "List"),
                  (438, 605, "Cost"), (484, 605, "Core"), (531, 605, "Total")] + az.ITEMS[10:19] + az.FOOTER
        fill = lambda cells: [(x, y, t.replace("{invoice}", "1").replace("{po}", "x").replace("{total}", "114.66"))
                              for x, y, t in cells]
        inv = store_invoices.parse(az.pdf(pages=[fill(first), fill(second)]), PDF)
        self.assertEqual([i["parts"][0] for i in inv["items"]], ["TS34-31351B", "MS34-31422B"])
        self.assertEqual(inv["total"], 114.66)

    def test_other_files_are_not_read_as_invoices(self):
        self.assertIsNone(store_invoices.parse(receipt_pdf("PO: IAC-PO-0021"), PDF))
        self.assertIsNone(store_invoices.parse(JPEG, "image/jpeg"))
        self.assertIsNone(store_invoices.parse(b"%PDF-1.4 garbage stream\nxx\nendstream", PDF))
        self.assertIsNone(store_invoices.parse(az.pdf()[:400], PDF))

    def test_strings_with_escaped_parentheses_and_tm_placement(self):
        cells = store_invoices._cells(b"BT 1 0 0 1 72 700 Tm (Pay \\(now\\)) Tj T* ET q 1 0 0 1 10 20 cm BT 5 5 Td [(A) -20 (B)] TJ ET Q")
        self.assertEqual(cells, [(72.0, 700.0, "Pay (now)"), (15.0, 25.0, "AB")])

    def test_money(self):
        for raw, value in (("39.33", 39.33), ("$1,068.64", 1068.64), ("-5.00", -5.0), ("(5.00)", -5.0)):
            self.assertEqual(store_invoices.money(raw), value)
        for raw in ("2", "SKU-1", "", "1 of 1"):
            self.assertIsNone(store_invoices.money(raw))


class _Line:
    def __init__(self, id, description, unit_price, quantity=1, item_type="part", approval="approved"):
        self.id, self.description, self.unit_price, self.quantity = id, description, unit_price, quantity
        self.item_type, self.approval = item_type, approval

    @property
    def billable(self):
        return self.approval in (None, "approved")


class CompareTests(unittest.TestCase):
    def test_part_number_then_words_and_each_status(self):
        items = store_invoices.parse(az.pdf(), PDF)["items"]
        lines = [_Line(1, "Rear shocks MS34-31422B (pair)", 38.49, 2), _Line(2, "Front twintube shock absorbers", 45.00, 2),
                 _Line(3, "Timing chain kit", 640.00, approval="pending"), _Line(4, "Labor", 150, item_type="labor"),
                 _Line(5, "Battery Duralast Gold", 249.99)]
        got = {c["item"]["parts"][0]: (c["status"], c["line"].id if c["line"] else None)
               for c in store_invoices.compare(items, lines)}
        self.assertEqual(got, {"TS34-31351B": ("billed", 2), "MS34-31422B": ("under_cost", 1), "SZND536G": ("not_on_job", None),
                               "82180": ("not_on_job", None), "9-0738SWP": ("not_approved", 3), "78DT": ("billed", 5)})
        shocks = store_invoices.compare(items[1:2], lines)[0]
        self.assertEqual((shocks["billed_unit"], shocks["margin"]), (38.49, -0.84))

    def test_labor_and_misc_lines_never_count_as_billing_a_part(self):
        items = store_invoices.parse(az.pdf(), PDF)["items"][:1]
        self.assertEqual(store_invoices.compare(items, [_Line(1, "Install shock absorbers", 150, item_type="labor")])[0]["status"],
                         "not_on_job")


class UploadTests(JobPoAndReceiptTests):
    def setUp(self):
        super().setUp()
        parts_receipts._parsed.clear()

    def saved(self):
        with self.Session() as db:
            return [(r.repair_order_id, r.vendor, r.total, r.invoice_number, r.po_text)
                    for r in db.query(PartsReceipt).order_by(PartsReceipt.id)]

    def test_autozone_pdf_finds_its_job_and_keeps_its_lines(self):
        r = self.match("02718.pdf", az.pdf(), PDF)
        self.assertTrue(r.headers["location"].startswith("/ro/21?receipt=matched"), r.headers["location"])
        self.assertEqual(self.saved(), [(21, "AutoZone 09999", 868.64, "09999000123", "IAC-PO-0021")])
        with self.Session() as db:
            details = json.loads(db.query(PartsReceipt).one().details)
        self.assertEqual(len(details["items"]), 6)
        page = self.owner.get("/ro/21").text
        self.assertIn("Invoice 09999000123", page)
        self.assertIn("6 parts to check against this job", page)  # job 21 only bills a water pump
        self.assertIn("6 parts bought", page)
        self.assertIn("Cores AutoZone says are still owed", page)

    def test_job_page_flags_a_part_billed_under_cost_and_passes_billed_ones(self):
        with self.Session() as db:
            for desc, price in (("Rear shocks MS34-31422B", 38.49), ("Front shocks TS34-31351B", 45), ("Oil change 5W-30 dexos", 79.99),
                                ("RTV gasket maker", 12.99), ("Timing chain kit 9-0738SWP", 640), ("Battery 78DT", 249.99)):
                db.add(LineItem(repair_order_id=21, description=desc, item_type="part", quantity=1, unit_price=price,
                                approval="approved"))
            db.commit()
        self.upload(self.owner, 21, "inv.pdf", az.pdf(), PDF)
        page = self.owner.get("/ro/21").text
        # The shocks are billed 84 cents under what they cost. The box of oil is flagged too: it was bought on this
        # job's PO but only one oil change is billed, which is for the office to judge.
        self.assertIn("2 parts to check against this job", page)
        self.assertIn("(store &#36;39.33 each, billed &#36;38.49)", page)
        self.assertIn("(&#36;0.84 under cost each)", page)
        self.assertIn("(store &#36;149.99 each, billed &#36;79.99)", page)
        self.assertIn("(&#36;1.00 over cost each)", page)  # the RTV


    def test_the_same_invoice_is_never_saved_twice(self):
        self.match("a.pdf", az.pdf(), PDF)
        r = self.match("a-again.pdf", az.pdf(), PDF)
        self.assertIn("already%20saved%20on%20IAC-PO-0021", r.headers["location"])
        r = self.upload(self.owner, 19, "a.pdf", az.pdf(), PDF)
        self.assertIn("already%20saved%20on%20IAC-PO-0021", r.headers["location"])
        r = self.upload(self.owner, 21, "a.pdf", az.pdf(), PDF)
        self.assertIn("already%20saved%20on%20this%20job", r.headers["location"])
        self.assertEqual(len(self.saved()), 1)

    def test_a_po_that_names_no_job_waits_until_put_on_one(self):
        r = self.match("cad.pdf", az.pdf(po="05 CADILLAC"), PDF)
        self.assertEqual(r.headers["location"], "/ro/receipts?saved=unmatched#unmatched")
        self.assertEqual(self.saved(), [(None, "AutoZone 09999", 868.64, "09999000123", "05 CADILLAC")])
        page = self.owner.get("/ro/receipts").text
        self.assertIn("Not matched yet", page)
        self.assertIn("05 CADILLAC", page)
        self.assertEqual(self.owner.get("/ro/receipts/unmatched/1").content[:5], b"%PDF-")
        bad = self.owner.post("/ro/receipts/unmatched/1/assign", data={"po": "IAC-PO-0999"})
        self.assertIn("error=", bad.headers["location"])
        r = self.owner.post("/ro/receipts/unmatched/1/assign", data={"po": "19"})
        self.assertTrue(r.headers["location"].startswith("/ro/19?receipt=matched"))
        self.assertEqual(self.saved()[0][0], 19)
        self.assertEqual(self.owner.get("/ro/receipts/unmatched/1").status_code, 404)  # it's on a job now
        self.assertIn("Invoice 09999000123", self.owner.get("/ro/19").text)

    def test_removing_a_waiting_receipt(self):
        self.match("cad.pdf", az.pdf(po="ZACK"), PDF)
        r = self.owner.post("/ro/receipts/unmatched/1/delete")
        self.assertEqual(r.headers["location"], "/ro/receipts?saved=removed#unmatched")
        self.assertEqual(self.saved(), [])

    def test_a_photo_with_no_po_is_still_refused_not_parked(self):
        r = self.match("IMG.jpg", JPEG, "image/jpeg")
        self.assertIn("error=", r.headers["location"])
        self.assertEqual(self.saved(), [])

    def test_batch_of_receipts_each_goes_where_its_po_says(self):
        files = [("files", ("one.pdf", az.pdf(invoice="1001"), PDF)),
                 ("files", ("two.pdf", az.pdf(invoice="1002", po="18 RAM 1500"), PDF)),
                 ("files", ("three.pdf", az.pdf(invoice="1003", po="IAC-PO-0019"), PDF)),
                 ("files", ("dupe.pdf", az.pdf(invoice="1001"), PDF)),
                 ("files", ("notes.txt", b"hello", "text/plain")),
                 ("files", ("IMG.jpg", JPEG, "image/jpeg"))]
        r = self.owner.post("/ro/receipts", files=files)
        self.assertEqual(r.status_code, 200)
        self.assertIn("2 on jobs · 1 not matched yet · 3 not saved", r.text)
        self.assertIn("invoice 1001 is already saved on IAC-PO-0021", r.text)
        self.assertIn("isn&#39;t a photo or PDF", r.text)
        self.assertEqual([row[:4] for row in self.saved()],
                         [(21, "AutoZone 09999", 868.64, "1001"), (None, "AutoZone 09999", 868.64, "1002"),
                          (19, "AutoZone 09999", 868.64, "1003")])

    def test_batch_limits(self):
        many = [("files", (f"{n}.pdf", az.pdf(invoice=str(n)), PDF)) for n in range(16)]
        r = self.owner.post("/ro/receipts", files=many)
        self.assertIn("up%20to%2015", r.headers["location"])
        r = self.owner.post("/ro/receipts", data={"po": "IAC-PO-0021"}, files=many[:2])
        self.assertIn("one%20receipt", r.headers["location"])
        self.assertEqual(self.saved(), [])

    def test_tech_adds_a_store_pdf_but_sees_no_costs_or_waiting_list(self):
        tech = self.sign_in("crew")
        r = self.upload(tech, 21, "inv.pdf", az.pdf(), PDF)
        self.assertIn("receipt=saved", r.headers["location"])
        page = tech.get("/ro/21").text
        self.assertIn("1 receipt on file", page)
        for hidden in ("Invoice 09999000123", "39.33", "to check against this job", "868.64"):
            self.assertNotIn(hidden, page)
        self.match("cad.pdf", az.pdf(invoice="2", po="ZACK"), PDF)
        self.assertEqual(tech.get("/ro/receipts/unmatched/2").status_code, 403)
        self.assertEqual(tech.post("/ro/receipts/unmatched/2/assign", data={"po": "21"}).status_code, 403)
        self.assertEqual(tech.post("/ro/receipts/unmatched/2/delete").status_code, 403)

    def test_out_of_state_store_is_pointed_out(self):
        cells = [(x, y, "Raleigh, NC 27601" if t == "Phoenix, AZ 85000" else t) for x, y, t in az.HEADER]
        fill = lambda c: [(x, y, t.replace("{invoice}", "5").replace("{po}", "IAC-PO-0021").replace("{total}", "868.64"))
                          for x, y, t in c]
        self.match("nc.pdf", az.pdf(pages=[fill(cells) + az.ITEMS + fill(az.FOOTER)]), PDF)
        self.assertIn("Bought at a store out of state", self.owner.get("/ro/21").text)

    def test_before_the_line_columns_exist_receipts_work_as_before(self):
        with self.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_parts_receipts_invoice_number"))
            conn.execute(text("ALTER TABLE parts_receipts DROP COLUMN details"))
            conn.execute(text("ALTER TABLE parts_receipts DROP COLUMN invoice_number"))
        parts_receipts._lines_ready.clear()
        r = self.match("a.pdf", az.pdf(), PDF)  # the PO is still read off it
        self.assertTrue(r.headers["location"].startswith("/ro/21?receipt=matched"), r.headers["location"])
        r = self.match("cad.pdf", az.pdf(invoice="7", po="05 CADILLAC"), PDF)  # nowhere to park it yet
        self.assertIn("error=", r.headers["location"])
        page = self.owner.get("/ro/21")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Invoice 09999000123", page.text)  # read from the saved PDF instead
        self.assertNotIn("Not matched yet", self.owner.get("/ro/receipts").text)
        self.assertEqual(self.owner.post("/ro/21/receipts/1/move", data={"po": "19"}).status_code, 303)
        self.assertEqual(self.owner.post("/ro/19/receipts/1/delete").status_code, 303)


del JobPoAndReceiptTests  # run its tests once, in its own module

if __name__ == "__main__":
    unittest.main()
