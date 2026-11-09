"""Customers from Quo contacts and texts. Made-up data, private database, no live services."""
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import quo_import
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.main import app
from app.models import Customer, User
from app.routes import dispatch, internal_messaging

PASSWORD = "fixture-password-1"
KEY = "fixture-internal-key"
OWNER = "+16025550199"
DIRECTORY = {
    "complete": True,
    "contacts": [
        {"firstName": "Pat", "lastName": "Sample", "company": "", "phones": ["6025550100"]},  # already a customer
        {"firstName": "Dana", "lastName": "Lee", "company": "", "phones": ["6025550101", "6025550109"]},
        {"firstName": "", "lastName": "", "company": "MA Towing", "phones": ["4805550122"]},
        {"firstName": "Shop", "lastName": "Line", "company": "", "phones": ["4809144144"]},
    ],
    "texted": [
        {"phone": "6025550101", "inbound": 4, "outbound": 2, "lastMessageAt": "2026-11-05T10:00:00Z"},
        {"phone": "6235550133", "inbound": 1, "outbound": 0, "lastMessageAt": "2026-11-06T10:00:00Z"},
        {"phone": "6235550144", "inbound": 0, "outbound": 3, "lastMessageAt": "2026-11-01T10:00:00Z"},
        {"phone": "8005550100", "inbound": 2, "outbound": 0, "lastMessageAt": "2026-11-02T10:00:00Z"},
        {"phone": "6025550199", "inbound": 9, "outbound": 9, "lastMessageAt": "2026-11-07T10:00:00Z"},
    ],
}


class QuoImportTests(unittest.TestCase):
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
        for target, attr, value in ((internal_messaging, "INTERNAL_API_KEY", KEY), (dispatch, "OWNER_PHONE", OWNER),
                                    (quo_import, "OWNER_PHONE", OWNER)):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        with self.Session() as db:
            db.add_all([User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"),
                        User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"),
                        Customer(id=1, first_name="Pat", last_name="Sample", phone="(602) 555-0100")])
            db.add(DispatchItem(quo_conversation_id="CNdana", customer_phone="+16025550101", category="general",
                                summary="Brakes?", last_message_id="m0", status="closed"))
            db.commit()

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def rows(self):
        with self.Session() as db:
            return {r["phone"]: r for r in quo_import.preview(db, DIRECTORY)}

    def test_preview_ticks_contacts_and_texters_and_skips_the_rest(self):
        rows = self.rows()
        self.assertNotIn("6025550100", rows)  # existing customer
        self.assertNotIn("6025550109", rows)  # second number on the same contact
        self.assertTrue(rows["6025550101"]["suggested"])
        self.assertEqual(rows["6025550101"]["inbound"], 4)
        self.assertTrue(rows["4805550122"]["suggested"])  # saved contact, never texted
        self.assertTrue(rows["6235550133"]["suggested"])  # texted the shop
        self.assertTrue(rows["6235550144"]["suggested"])  # every real number is ticked
        self.assertEqual(rows["8005550100"]["skip"], "toll-free")
        self.assertEqual(rows["4809144144"]["skip"], "shop or owner number")
        self.assertEqual(rows["6025550199"]["skip"], "shop or owner number")

    def test_import_adds_only_chosen_valid_rows_once_and_links_history(self):
        with self.Session() as db:
            rows = quo_import.preview(db, DIRECTORY)
            chosen = {"6025550101", "4805550122", "6235550133", "8005550100", "6025550100"}
            self.assertEqual(quo_import.import_rows(db, rows, chosen, "owner"), 3)
            self.assertEqual(quo_import.import_rows(db, quo_import.preview(db, DIRECTORY), chosen, "owner"), 0)
            people = {c.phone: c for c in db.query(Customer).all()}
            self.assertEqual(len(people), 4)
            dana = people["+16025550101"]
            self.assertEqual((dana.first_name, dana.last_name), ("Dana", "Lee"))
            towing = people["+14805550122"]
            self.assertEqual((towing.customer_type, towing.fleet_name, towing.first_name), ("fleet", "MA Towing", "MA Towing"))
            self.assertEqual(people["+16235550133"].first_name, "Unknown")
            self.assertEqual(db.query(DispatchItem).one().customer_id, dana.id)
            self.assertEqual(db.get(Customer, 1).first_name, "Pat")  # existing untouched

    def test_import_page_is_owner_only_and_saves_ticked_rows(self):
        with patch.object(quo_import, "fetch_directory", lambda: (DIRECTORY, None)):
            office = TestClient(app, follow_redirects=False)
            office.post("/login", data={"username": "office", "password": PASSWORD})
            self.assertEqual(office.get("/customers/import").status_code, 403)
            self.assertEqual(office.post("/customers/import", data={"phone": "6025550101"}).status_code, 403)
            owner = TestClient(app, follow_redirects=False)
            owner.post("/login", data={"username": "owner", "password": PASSWORD})
            page = owner.get("/customers/import").text
            self.assertIn("Dana Lee", page)
            self.assertIn("Skipped: toll-free", page)
            done = owner.post("/customers/import", data={"phone": ["6025550101", "6235550144"]})
            self.assertEqual(done.headers["location"], "/customers/import?done=2")
            self.assertIn("Added 2 customers", owner.get("/customers/import?done=2").text)
        with self.Session() as db:
            self.assertEqual(db.query(Customer).count(), 3)

    def test_quo_down_saves_nothing(self):
        with patch.object(quo_import, "fetch_directory", lambda: (None, "Quo couldn't be read right now.")):
            owner = TestClient(app, follow_redirects=False)
            owner.post("/login", data={"username": "owner", "password": PASSWORD})
            self.assertIn("be read right now", owner.get("/customers/import").text)
            owner.post("/customers/import", data={"phone": "6025550101"})
        with self.Session() as db:
            self.assertEqual(db.query(Customer).count(), 1)

    def test_a_new_texter_gets_a_customer_file(self):
        client = TestClient(app)
        headers = {"X-Internal-Key": KEY}
        body = {"conversationId": "CNnew", "messageId": "n1", "phone": "+16235550177", "body": "Need an oil change", "contactName": "Sam Rivera"}
        self.assertTrue(client.post("/internal/dispatch/intake", headers=headers, json=body).json()["recorded"])
        client.post("/internal/dispatch/intake", headers=headers, json={**body, "messageId": "n2"})
        with self.Session() as db:
            sam = db.query(Customer).filter(Customer.phone == "+16235550177").all()
            self.assertEqual(len(sam), 1)
            self.assertEqual((sam[0].first_name, sam[0].last_name), ("Sam", "Rivera"))
            item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == "CNnew").one()
            self.assertEqual(item.customer_id, sam[0].id)
        client.post("/internal/dispatch/intake", headers=headers,
                    json={"conversationId": "CNtoll", "messageId": "t1", "phone": "+18885550100", "body": "Your car warranty"})
        with self.Session() as db:
            self.assertEqual(db.query(Customer).filter(Customer.phone == "+18885550100").count(), 0)


if __name__ == "__main__":
    unittest.main()
