"""Tags on jobs and customers: add, show, search, remove, and stay quiet before the table exists."""
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import tags
from app.database import Base, get_db
from app.main import app
from app.models import Customer, RepairOrder, User, Vehicle
from app.auth import hash_password


class TagTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        db.add(User(id=1, username="owner", password_hash=hash_password("pw-owner-123"), role="owner", full_name="Owner"))
        db.add(Customer(id=1, first_name="Pat", last_name="Sample", phone="+16025550100"))
        db.add(Customer(id=2, first_name="Lee", last_name="Other"))
        db.add(Vehicle(id=1, customer_id=1, year=2015, make="Ford", model="Explorer"))
        db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="intake", public_token="t1"))
        db.commit()
        db.close()

        def override():
            s = self.Session()
            try:
                yield s
            finally:
                s.close()
        app.dependency_overrides[get_db] = override
        self.read_only, app.state.read_only = app.state.read_only, False
        self.client = TestClient(app)
        r = self.client.post("/login", data={"username": "owner", "password": "pw-owner-123"}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)
        app.state.read_only = self.read_only

    def test_clean_keeps_short_plain_words(self):
        self.assertEqual(tags.clean("  #TikTok_Lead!! "), "tiktok lead")
        self.assertEqual(len(tags.clean("x" * 80)), tags.MAX_LEN)

    def test_add_show_search_remove(self):
        r = self.client.post("/tags/add", data={"kind": "job", "target_id": 1, "name": "Comeback, oil leak", "next": "/ro/1"},
                             headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/ro/1")
        self.client.post("/tags/add", data={"kind": "customer", "target_id": 1, "name": "fleet"},
                         headers={"Origin": "http://testserver"})
        page = self.client.get("/ro/1").text
        self.assertIn("#comeback", page)
        self.assertIn("#oil leak", page)
        self.assertIn("Customer tags:", page)
        self.assertIn("#comeback", self.client.get("/dashboard").text)  # Home job rows carry tags
        found = self.client.get("/tags/?q=oil").text
        self.assertIn("#1 2015 Ford Explorer", found)
        self.assertIn("No customers with that tag.", found)
        self.assertIn("#1", self.client.get("/ro/?status=all&search=comeback").text)
        self.assertIn("/ro/1", self.client.get("/ro/?status=all&search=fleet").text)  # a customer's tag finds their jobs
        customers = self.client.get("/customers/?search=fleet").text
        self.assertIn("Pat", customers)
        self.assertNotIn("Lee", customers)
        self.client.post("/tags/remove", data={"kind": "job", "target_id": 1, "name": "comeback"},
                         headers={"Origin": "http://testserver"})
        self.assertNotIn("#comeback", self.client.get("/ro/1").text)

    def test_bad_tag_and_off_site_next_are_refused(self):
        r = self.client.post("/tags/add", data={"kind": "job", "target_id": 1, "name": "!!!", "next": "//evil.example"},
                             headers={"Origin": "http://testserver"}, follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("/ro/1?tag_error="))
        self.assertEqual(self.client.post("/tags/add", data={"kind": "job", "target_id": 99, "name": "x"},
                                          headers={"Origin": "http://testserver"}).status_code, 404)

    def test_missing_table_means_no_tags_not_errors(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name != "tags"])
        db = sessionmaker(bind=engine)()
        self.assertFalse(tags.ready(db))
        self.assertEqual(tags.names_for(db, "job", [1]), {})
        self.assertEqual(tags.matching(db, "job", "fleet"), set())
        self.assertEqual(tags.add(db, "job", 1, "fleet", "owner"), "Tags aren't switched on yet.")
        db.close()


if __name__ == "__main__":
    unittest.main()
