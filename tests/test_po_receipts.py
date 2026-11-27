"""Parts PO numbers on every job, and parts store receipts kept on jobs and matched by PO. Made-up data only."""
import io
import unittest

from test_agent_api import PASSWORD, _Setup

from app import estimate_pdf, parts_receipts, po_numbers
from app.auth import hash_password
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.parts_receipts import PartsReceipt
from app.permissions import allowed

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def receipt_pdf(text: str, compress: bool = True) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter, pageCompression=1 if compress else 0)
    c.drawString(72, 720, "AUTOZONE #2718 COMMERCIAL")
    c.drawString(72, 700, text)
    c.drawString(72, 680, "INVOICE 4471-118 TOTAL 84.99")
    c.save()
    return out.getvalue()


class PoNumberTests(unittest.TestCase):
    def test_po_comes_from_the_job_number(self):
        self.assertEqual(po_numbers.for_job(21), "IAC-PO-0021")
        self.assertEqual(po_numbers.for_job(12345), "IAC-PO-12345")
        self.assertEqual(po_numbers.for_job(None), "")

    def test_reads_back_the_ways_stores_print_it(self):
        for text in ("IAC-PO-0021", "iac po 21", "IACPO0021", "PO# 0021", "P.O. 21", "PO: 21", "po 21",
                     "Purchase order 21", "IAC-PO-21 thanks"):
            self.assertEqual(po_numbers.parse(text), 21, text)
        for text in ("21", "Pat", "SPOKE 21", "POWER 21", "", None, "PO", "IAC-PO-0000"):
            self.assertIsNone(po_numbers.parse(text), text)

    def test_receipt_text_counts_only_our_own_prefix(self):
        self.assertEqual(po_numbers.find_in_text("PO 1234\nIAC-PO-0021\nREF IAC PO 19 iac-po-0021"), [21, 19])
        self.assertEqual(po_numbers.find_in_text("PO 1234 ORDER 55"), [])


class JobPoAndReceiptTests(_Setup):
    def setUp(self):
        super().setUp()
        parts_receipts._ready.clear()
        with self.Session() as db:
            db.add(User(id=3, username="crew", password_hash=hash_password(PASSWORD), role="tech"))
            c = Customer(first_name="Pat", last_name="Example", phone="6025550100")
            db.add(c)
            db.flush()
            v = Vehicle(customer_id=c.id, year=2005, make="Cadillac", model="Escalade")
            db.add(v)
            db.flush()
            for job_id in (19, 21):
                db.add(RepairOrder(id=job_id, customer_id=c.id, vehicle_id=v.id, status="in_progress",
                                   public_token=str(job_id) * 20))
            db.flush()
            db.add(LineItem(repair_order_id=21, description="Water pump", item_type="part", quantity=1,
                            unit_price=180, approval="approved"))
            db.commit()
        self.owner = self.sign_in("owner")

    def tearDown(self):
        parts_receipts._ready.clear()
        super().tearDown()

    def receipts(self):
        with self.Session() as db:
            return [(r.repair_order_id, r.vendor, r.total, r.matched_by, r.content_type, r.uploaded_by)
                    for r in db.query(PartsReceipt).order_by(PartsReceipt.id)]

    def upload(self, client, ro_id, name, data, kind, **form):
        return client.post(f"/ro/{ro_id}/receipts", data=form, files=[("files", (name, data, kind))])

    def match(self, name, data, kind, **form):
        return self.owner.post("/ro/receipts", data=form, files=[("files", (name, data, kind))])

    # ------------------------------------------------------------ PO everywhere

    def test_every_job_has_a_po_on_its_page_board_pdf_and_assistant_read(self):
        with self.Session() as db:
            ro = db.get(RepairOrder, 21)
            self.assertEqual(ro.po_number, "IAC-PO-0021")
            pdf = estimate_pdf.build(ro)
        self.assertIn("IAC-PO-0021", parts_receipts.pdf_text(pdf))
        self.assertIn("IAC-PO-0021", self.owner.get("/ro/21").text)
        self.assertIn("IAC-PO-0021", self.owner.get("/dashboard").text)
        _, headers = self.issue()
        job = self.client.get("/internal/agent/v1/jobs/21", headers=headers).json()
        self.assertEqual(job["po_number"], "IAC-PO-0021")

    def test_new_jobs_get_a_po_as_soon_as_they_exist(self):
        with self.Session() as db:
            ro = RepairOrder(customer_id=1, vehicle_id=1, public_token="n" * 40)
            db.add(ro)
            db.commit()
            self.assertEqual(ro.po_number, po_numbers.for_job(ro.id))

    def test_searching_a_po_opens_its_job_even_when_closed(self):
        with self.Session() as db:
            db.get(RepairOrder, 19).status = "delivered"
            db.commit()
        r = self.owner.get("/ro/", params={"search": "PO# 0019"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(r.headers["location"].startswith("/ro/19?po_found="))
        self.assertIn("Found by PO", self.owner.get(r.headers["location"]).text)
        self.assertEqual(self.owner.get("/ro/", params={"search": "IAC-PO-0999"}).status_code, 200)
        listing = self.owner.get("/ro/", params={"search": "21"}).text
        self.assertIn("/ro/21", listing)

    # ------------------------------------------------------------ receipts on a job

    def test_receipt_saved_from_the_job_page_and_opened_by_the_office(self):
        r = self.upload(self.owner, 21, "IMG_0042.jpg", JPEG, "image/jpeg", vendor="AutoZone", total="$84.99")
        self.assertIn("receipt=saved", r.headers["location"])
        self.assertEqual(self.receipts(), [(21, "AutoZone", 84.99, "job_page", "image/jpeg", "owner")])
        page = self.owner.get("/ro/21").text
        self.assertIn("AutoZone", page)
        self.assertIn("&#36;84.99", page)
        got = self.owner.get("/ro/21/receipts/1")
        self.assertEqual((got.status_code, got.content), (200, JPEG))
        self.assertIn("no-store", got.headers["cache-control"])
        self.assertEqual(self.owner.get("/ro/19/receipts/1").status_code, 404)

    def test_tech_can_add_a_receipt_but_not_open_one_or_see_prices(self):
        tech = self.sign_in("crew")
        self.assertTrue(allowed("tech", "POST", "/ro/21/receipts"))
        self.assertFalse(allowed("tech", "GET", "/ro/21/receipts/1"))
        self.assertFalse(allowed("tech", "GET", "/ro/receipts"))
        r = self.upload(tech, 21, "r.jpg", JPEG, "image/jpeg", vendor="O'Reilly", total="40")
        self.assertIn("receipt=saved", r.headers["location"])
        self.assertEqual(tech.get("/ro/21/receipts/1").status_code, 403)
        page = tech.get("/ro/21").text
        self.assertIn("1 receipt on file", page)
        self.assertNotIn("&#36;40.00", page)

    def test_bad_files_are_refused_and_nothing_is_saved(self):
        r = self.upload(self.owner, 21, "notes.txt", b"hello", "image/jpeg")
        self.assertIn("receipt_error=", r.headers["location"])
        r = self.upload(self.owner, 21, "r.jpg", JPEG, "image/jpeg", total="abc")
        self.assertIn("receipt_error=", r.headers["location"])
        self.assertEqual(self.receipts(), [])

    def test_job_page_warns_when_the_pdf_shows_another_jobs_po(self):
        r = self.upload(self.owner, 21, "receipt.pdf", receipt_pdf("PO: IAC-PO-0019"), "application/pdf")
        self.assertIn("other=IAC-PO-0019", r.headers["location"])
        self.assertEqual(self.receipts()[0][0], 21)

    def test_move_and_remove(self):
        self.upload(self.owner, 21, "r.jpg", JPEG, "image/jpeg")
        r = self.owner.post("/ro/21/receipts/1/move", data={"po": "IAC-PO-0019"})
        self.assertTrue(r.headers["location"].startswith("/ro/19?receipt=moved"))
        self.assertEqual(self.receipts()[0][0], 19)
        r = self.owner.post("/ro/19/receipts/1/move", data={"po": "PO 777"})
        self.assertIn("receipt_error=", r.headers["location"])
        self.owner.post("/ro/19/receipts/1/delete")
        self.assertEqual(self.receipts(), [])

    # ------------------------------------------------------------ match by PO

    def test_store_pdf_finds_its_job_by_the_po_printed_on_it(self):
        for compress in (True, False):
            r = self.match("autozone.pdf", receipt_pdf("CUSTOMER PO: IAC-PO-0021", compress), "application/pdf",
                           vendor="AutoZone", total="84.99")
            self.assertTrue(r.headers["location"].startswith("/ro/21?receipt=matched"), r.headers["location"])
        self.assertEqual([row[:4] for row in self.receipts()],
                         [(21, "AutoZone", 84.99, "po_on_receipt")] * 2)

    def test_typed_po_matches_a_photo(self):
        r = self.match("IMG_1.jpg", JPEG, "image/jpeg", po="po 19")
        self.assertTrue(r.headers["location"].startswith("/ro/19?receipt=matched"))
        self.assertEqual(self.receipts()[0][3], "po_typed")

    def test_no_match_saves_nothing_and_says_why(self):
        for form, data, kind in (({}, JPEG, "image/jpeg"), ({"po": "IAC-PO-0999"}, JPEG, "image/jpeg"),
                                 ({"po": "hello"}, JPEG, "image/jpeg"),
                                 ({}, receipt_pdf("IAC-PO-0019 and IAC-PO-0021"), "application/pdf")):
            r = self.match("r", data, kind, **form)
            self.assertTrue(r.headers["location"].startswith("/ro/receipts?error="), r.headers["location"])
        self.assertEqual(self.receipts(), [])
        self.assertEqual(self.owner.get("/ro/receipts").status_code, 200)

    def test_without_the_table_everything_else_still_works(self):
        PartsReceipt.__table__.drop(self.engine)
        parts_receipts._ready.clear()
        page = self.owner.get("/ro/21")
        self.assertEqual(page.status_code, 200)
        self.assertIn("aren't switched on yet", page.text)
        r = self.upload(self.owner, 21, "r.jpg", JPEG, "image/jpeg")
        self.assertIn("receipt_error=", r.headers["location"])
        self.assertIn("switched on", self.owner.get("/ro/receipts").text)


if __name__ == "__main__":
    unittest.main()
