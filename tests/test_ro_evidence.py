"""Recovery B: per-line completion and RO photos/files. Synthetic data, private database."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, Invoice, LineItem, RepairOrder, RoAttachment, User, Vehicle
from app.routes import ro_evidence

PASSWORD = "fixture-password-1"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


class RoEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        with self.Session() as db:
            for uid, name, role in ((1, "owner", "owner"), (2, "crew", "tech"), (3, "desk", "office")):
                db.add(User(id=uid, username=name, password_hash=hash_password(PASSWORD), role=role))
            customer = Customer(first_name="Antwon", last_name="Test", phone="+16025550108")
            vehicle = Vehicle(customer=customer, year=2009, make="Chevrolet", model="Impala")
            ro = RepairOrder(id=8, customer=customer, vehicle=vehicle, status="in_progress", concern="x", public_token="synthetic-8")
            other = RepairOrder(id=9, customer=customer, vehicle=vehicle, status="in_progress", concern="y", public_token="synthetic-9")
            db.add_all([customer, vehicle, ro, other])
            for lid, desc, approval, price in ((1, "Headlight bulb", "approved", 40), (2, "Lower radiator hose", "approved", 180),
                                               (3, "Oil leak diagnosis", None, 120), (4, "Valve cover gasket", "declined", 400),
                                               (5, "Tie rods", "pending", 300)):
                db.add(LineItem(id=lid, repair_order=ro, description=desc, approval=approval, quantity=1, unit_price=price))
            db.commit()
        self.owner, self.tech, self.office = (self.login(n) for n in ("owner", "crew", "desk"))
        self.public = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def login(self, username):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": username, "password": PASSWORD})
        return client

    def line(self, lid):
        with self.Session() as db:
            li = db.get(LineItem, lid)
            return li.completed_at, li.completed_by, li.approval, li.unit_price

    def upload(self, client, files, **form):
        return client.post("/ro/8/attachments", files=[("files", f) for f in files], data=form)

    # Per-line completion
    def test_tech_marks_approved_lines_done_without_touching_price_approval_or_status(self):
        self.assertEqual(self.tech.post("/ro/8/lines/1/complete").status_code, 303)
        done_at, by, approval, price = self.line(1)
        self.assertIsNotNone(done_at)
        self.assertEqual((by, approval, price), ("crew", "approved", 40))
        with self.Session() as db:
            ro = db.get(RepairOrder, 8)
            self.assertEqual(ro.status, "in_progress")
            self.assertEqual(ro_evidence.progress(ro), {"done": 1, "total": 3, "all_done": False})
            self.assertEqual(db.query(Invoice).count(), 0)
        self.tech.post("/ro/8/lines/1/complete")  # double tap keeps the first time
        self.assertEqual(self.line(1)[0], done_at)

    def test_unapproved_lines_cannot_be_marked_done(self):
        for lid in (4, 5):
            self.assertEqual(self.tech.post(f"/ro/8/lines/{lid}/complete").status_code, 409)
            self.assertIsNone(self.line(lid)[0])
        self.assertEqual(self.tech.post("/ro/9/lines/1/complete").status_code, 404)  # line from another job

    def test_partial_then_whole_job_completion_shows_on_the_page_and_survives_refresh(self):
        self.tech.post("/ro/8/lines/1/complete")
        page = self.owner.get("/ro/8").text
        self.assertIn("1 of 3 done", page)
        self.assertIn('aria-label="Reopen Headlight bulb"', page)
        self.assertIn("Waiting on approval", page)
        for lid in (2, 3):
            self.tech.post(f"/ro/8/lines/{lid}/complete")
        page = self.owner.get("/ro/8").text
        self.assertIn("3 of 3 done", page)
        self.assertIn("all work done", page)
        self.tech.post("/ro/8/lines/2/reopen")
        self.assertIn("2 of 3 done", self.owner.get("/ro/8").text)

    def test_signed_out_users_are_sent_to_login(self):
        self.assertEqual(self.public.post("/ro/8/lines/1/complete").headers["location"], "/login")
        self.assertEqual(self.public.get("/ro/8/attachments/1").headers["location"], "/login")

    def test_done_or_customer_seen_lines_cannot_be_deleted_and_techs_cannot_delete(self):
        self.assertEqual(self.tech.post("/ro/8/delete-line-item/3").status_code, 403)
        self.assertEqual(self.owner.post("/ro/8/delete-line-item/1").status_code, 409)  # approved by customer
        self.owner.post("/ro/8/lines/3/complete")
        self.assertEqual(self.owner.post("/ro/8/delete-line-item/3").status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(LineItem).count(), 5)

    # Uploads
    def test_phone_upload_saves_to_the_line_and_stays_internal(self):
        response = self.upload(self.tech, [("IMG_0001.jpg", JPEG, "image/jpeg"), ("IMG_0002.HEIC", HEIC, "application/octet-stream")],
                               line_item_id="1", visibility="customer")
        self.assertIn("upload=2", response.headers["location"])
        with self.Session() as db:
            rows = db.query(RoAttachment).order_by(RoAttachment.id).all()
            self.assertEqual([(r.line_item_id, r.content_type, r.visibility, r.uploaded_by) for r in rows],
                             [(1, "image/jpeg", "internal", "crew"), (1, "image/heic", "internal", "crew")])
        page = self.owner.get("/ro/8").text
        self.assertIn("IMG_0001.jpg", page)
        self.assertIn("Headlight bulb · crew", page)
        self.assertIn("Shop only", page)
        got = self.tech.get("/ro/8/attachments/1")
        self.assertEqual((got.status_code, got.content, got.headers["content-type"]), (200, JPEG, "image/jpeg"))
        self.assertIn("no-store", got.headers["cache-control"])

    def test_bad_files_are_rejected_with_a_reason_and_nothing_partial_is_hidden(self):
        r = self.upload(self.owner, [("notes.txt", b"hello there", "image/jpeg")])
        self.assertIn("upload=failed", r.headers["location"])
        self.assertIn("not+a+photo" if "+" in r.headers["location"] else "not%20a%20photo", r.headers["location"])
        r = self.upload(self.owner, [("big.jpg", JPEG + b"\x00" * ro_evidence.MAX_BYTES, "image/jpeg"), ("ok.pdf", PDF, "application/pdf")])
        self.assertIn("upload=1", r.headers["location"])
        self.assertIn("over%2010%20MB", r.headers["location"])
        with self.Session() as db:
            self.assertEqual([a.filename for a in db.query(RoAttachment).all()], ["ok.pdf"])
        self.assertEqual(self.upload(self.owner, [("a.jpg", JPEG, "image/jpeg")], line_item_id="99").status_code, 404)
        self.assertEqual(self.upload(self.owner, [("a.jpg", JPEG, "image/jpeg")], visibility="everyone").status_code, 422)

    def test_customer_sees_only_files_the_office_shares_on_their_own_job(self):
        self.upload(self.tech, [("inside.jpg", JPEG, "image/jpeg")])
        self.upload(self.office, [("finished.png", PNG, "image/png")], visibility="customer", line_item_id="1")
        status = self.public.get("/status/synthetic-8").text
        self.assertIn("/status/synthetic-8/files/2", status)
        self.assertNotIn("/files/1", status)
        self.assertEqual(self.public.get("/status/synthetic-8/files/2").content, PNG)
        self.assertEqual(self.public.get("/status/synthetic-8/files/1").status_code, 404)  # internal
        self.assertEqual(self.public.get("/status/synthetic-9/files/2").status_code, 404)  # other job's token
        self.assertEqual(self.public.get("/status/wrong/files/2").status_code, 404)
        # Techs can't share; the office can share and unshare.
        self.assertEqual(self.tech.post("/ro/8/attachments/1/visibility", data={"visibility": "customer"}).status_code, 403)
        self.office.post("/ro/8/attachments/2/visibility", data={"visibility": "internal"})
        self.assertEqual(self.public.get("/status/synthetic-8/files/2").status_code, 404)
        self.assertNotIn("Photos from the shop", self.public.get("/status/synthetic-8").text)

    def test_files_are_scoped_to_their_job(self):
        self.upload(self.owner, [("a.jpg", JPEG, "image/jpeg")])
        self.assertEqual(self.owner.get("/ro/9/attachments/1").status_code, 404)

    def test_sniffing(self):
        self.assertEqual(ro_evidence.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(ro_evidence.sniff(b"<html><script>"))


if __name__ == "__main__":
    unittest.main()
