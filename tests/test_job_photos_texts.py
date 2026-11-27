"""Inspection photos on the approval page and text, and the read-only texts card on the job page.
Synthetic data, private in-memory database, no live services."""
import re
import unittest
from datetime import datetime, timedelta

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import approval_links
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Communication, Customer, LineItem, RepairOrder, RoAttachment, User, Vehicle

PASSWORD = "fixture-password-1"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64


class JobPhotosAndTextsTests(unittest.TestCase):
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
            customer = Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550108")
            vehicle = Vehicle(customer=customer, year=2016, make="Ford", model="Focus")
            ro = RepairOrder(id=8, customer=customer, vehicle=vehicle, status="waiting_approval", concern="x",
                             public_token="synthetic-8")
            other = RepairOrder(id=9, customer=customer, vehicle=vehicle, status="delivered", concern="y",
                                public_token="synthetic-9")
            db.add_all([customer, vehicle, ro, other])
            db.add_all([LineItem(id=1, repair_order=ro, description="Front pads", approval="pending", quantity=1, unit_price=200),
                        LineItem(id=2, repair_order=ro, description="Rotors", approval="pending", quantity=1, unit_price=150)])
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

    def upload(self, client, files, **form):
        return client.post("/ro/8/attachments", files=[("files", f) for f in files], data=form)

    def visibilities(self):
        with self.Session() as db:
            return [(a.filename, a.visibility) for a in db.query(RoAttachment).order_by(RoAttachment.id)]

    # Photos: upload and the Show customer toggle
    def test_job_page_has_camera_and_library_pickers(self):
        page = self.owner.get("/ro/8").text
        self.assertIn('capture="environment"', page)
        self.assertRegex(page, r'id="ev-files"[^>]*multiple')
        self.assertIn("Take photo", page)
        self.assertIn("Add photos", page)
        self.assertRegex(page, r'name="visibility" value="customer" checked')  # Show customer starts on
        tech_page = self.tech.get("/ro/8").text
        self.assertIn("Take photo", tech_page)
        self.assertNotIn('name="visibility" value="customer" checked', tech_page)
        self.assertIn("stay shop only until the office", tech_page)

    def test_show_customer_checkbox_and_toggle(self):
        r = self.upload(self.office, [("brakes.jpg", JPEG, "image/jpeg")], visibility="customer")
        self.assertTrue(r.headers["location"].endswith("#evidence"))
        self.upload(self.office, [("receipt.pdf", PDF, "application/pdf")])  # box unticked: nothing sent
        self.upload(self.tech, [("tech.jpg", JPEG, "image/jpeg")], visibility="customer")  # techs can't share
        self.assertEqual(self.visibilities(), [("brakes.jpg", "customer"), ("receipt.pdf", "internal"), ("tech.jpg", "internal")])
        self.assertEqual(self.tech.post("/ro/8/attachments/3/visibility", data={"visibility": "customer"}).status_code, 403)
        r = self.office.post("/ro/8/attachments/3/visibility", data={"visibility": "customer"})
        self.assertTrue(r.headers["location"].endswith("#file-3"))
        self.owner.post("/ro/8/attachments/1/visibility", data={"visibility": "internal"})
        self.assertEqual(self.visibilities(), [("brakes.jpg", "internal"), ("receipt.pdf", "internal"), ("tech.jpg", "customer")])
        page = self.owner.get("/ro/8").text
        self.assertIn("Hide from customer", page)
        self.assertIn("1 shown to the customer", page)

    def test_unused_picker_posts_an_empty_part_that_is_ignored(self):
        r = self.upload(self.owner, [("", b"", "application/octet-stream"), ("a.jpg", JPEG, "image/jpeg")], visibility="customer")
        self.assertIn("upload=1", r.headers["location"])
        self.assertNotIn("why=", r.headers["location"])
        self.assertEqual(self.visibilities(), [("a.jpg", "customer")])

    # Photos on the approval page
    def test_approval_page_shows_only_customer_photos_by_their_item(self):
        self.upload(self.office, [("overview.jpg", JPEG, "image/jpeg")], visibility="customer")            # 1: gallery
        self.upload(self.office, [("pads.png", PNG, "image/png")], visibility="customer", line_item_id="1")  # 2: under pads
        self.upload(self.office, [("shop.jpg", JPEG, "image/jpeg")], line_item_id="1")                       # 3: internal
        self.upload(self.tech, [("crew.jpg", JPEG, "image/jpeg")], visibility="customer")                    # 4: internal
        page = self.public.get("/sign/ro/synthetic-8").text
        self.assertIn("Photos from the inspection", page)
        for shown in (1, 2):
            self.assertIn(f"/status/synthetic-8/files/{shown}", page)
        for hidden in (3, 4):
            self.assertNotIn(f"/files/{hidden}\"", page)
        gallery = page.split('id="inspection-photos"')[1].split('id="sign-form"')[0]
        self.assertIn("/files/1", gallery)
        self.assertNotIn("/files/2", gallery)
        pads_row = page.split('aria-label="Photos for Front pads"')[1].split("Rotors")[0]
        self.assertIn("/status/synthetic-8/files/2", pads_row)
        self.assertEqual(self.public.get("/status/synthetic-8/files/2").content, PNG)
        self.assertEqual(self.public.get("/status/synthetic-8/files/3").status_code, 404)

    def test_approval_page_without_photos_has_no_gallery(self):
        self.upload(self.office, [("shop.jpg", JPEG, "image/jpeg")])
        page = self.public.get("/sign/ro/synthetic-8").text
        self.assertIn("Authorize Repair Work", page)
        self.assertNotIn("inspection-photos", page)
        self.assertNotIn("/files/", page)

    # Text wording
    def text(self):
        with self.Session() as db:
            return approval_links.message_for(db.get(RepairOrder, 8))

    def test_text_mentions_photos_only_when_customer_photos_exist(self):
        self.assertNotIn("Photos", self.text())
        self.upload(self.office, [("shop.jpg", JPEG, "image/jpeg")])                       # shop only
        self.upload(self.office, [("quote.pdf", PDF, "application/pdf")], visibility="customer")  # not a photo
        self.assertNotIn("Photos", self.text())
        self.upload(self.office, [("pads.jpg", JPEG, "image/jpeg")], visibility="customer")
        text = self.text()
        self.assertIn("/status/synthetic-8 Photos from the inspection are included. Review the itemized", text)
        self.assertEqual(text.count(approval_links.PHOTOS_LINE), 1)
        self.assertTrue(text.startswith("Hi Pat, your estimate for the 2016 Ford Focus is ready. Open your vehicle status"))

    # Texts card
    def add_texts(self, rows):
        with self.Session() as db:
            db.add_all(rows)
            db.commit()

    def test_texts_card_shows_the_conversation_read_only(self):
        start = datetime(2026, 11, 20, 20, 5)  # UTC; 1:05 PM in Phoenix
        self.add_texts([
            Communication(customer_id=1, repair_order_id=9, direction="inbound", body="Old job question", created_at=start - timedelta(days=30)),
            Communication(customer_id=1, repair_order_id=8, direction="outbound", body="Your estimate is ready", created_at=start),
            Communication(customer_id=1, repair_order_id=None, direction="inbound", body="Can I come at 3?", created_at=start + timedelta(minutes=5)),
            Communication(customer_id=2, repair_order_id=None, direction="inbound", body="Someone else entirely", created_at=start),
        ])
        page = self.owner.get("/ro/8").text
        card = page.split('id="texts"')[1].split('id="texts-thread"')[0] + page.split('id="texts-thread"')[1].split("Read only")[0]
        self.assertIn("Texts with Pat", page)
        self.assertNotIn("Someone else entirely", page)
        order = [card.index(t) for t in ("Old job question", "Your estimate is ready", "Can I come at 3?")]
        self.assertEqual(order, sorted(order))  # newest at the bottom
        self.assertRegex(card, r'data-direction="outbound"[\s\S]*Your estimate is ready')
        self.assertEqual(len(re.findall(r'data-direction="inbound"', card)), 2)
        self.assertIn("Nov 20, 1:05 PM", card)
        self.assertEqual(card.count("data-this-job"), 1)
        self.assertIn('href="/ro/9"', card)
        self.assertIn("3 total · 1 on this job", page)
        self.assertNotIn("Show all", card)
        self.assertNotRegex(card, r"<form|<textarea")  # read only

    def test_texts_card_keeps_the_last_30_open_and_the_rest_behind_show_all(self):
        start = datetime(2026, 11, 1, 18, 0)
        self.add_texts([Communication(customer_id=1, repair_order_id=8, direction="inbound" if i % 2 else "outbound",
                                      body=f"message number {i:02d}", created_at=start + timedelta(minutes=i)) for i in range(35)])
        page = self.owner.get("/ro/8").text
        self.assertIn("Show all 35 texts", page)
        older = page.split("Show all 35 texts")[1].split("</details>")[0]
        self.assertIn("message number 04", older)
        self.assertNotIn("message number 05", older)
        after = page.split("</details>", 1)[1]
        self.assertIn("message number 05", after)
        self.assertIn("message number 34", after)

    def test_techs_see_the_texts_card_as_before_and_empty_state(self):
        self.assertIn("No texts with Pat saved yet.", self.owner.get("/ro/8").text)
        self.add_texts([Communication(customer_id=1, repair_order_id=8, direction="inbound", body="Thanks!",
                                      created_at=datetime(2026, 11, 20, 20, 0))])
        page = self.tech.get("/ro/8")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Texts with Pat", page.text)
        self.assertIn("Thanks!", page.text)


if __name__ == "__main__":
    unittest.main()
