"""Role limits and staff login rules, on made-up accounts in a private database."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import User
from app.permissions import allowed
from app.security import limiter

PASSWORD = "fixture-password-1"
OTHER = "fixture-password-2"


class RoleTableTests(unittest.TestCase):
    def test_finance_and_logins_are_owner_only(self):
        for role in ("admin", "office", "tech", None):
            self.assertFalse(allowed(role, "GET", "/finance/"), role)
            self.assertFalse(allowed(role, "POST", "/settings/users/create"), role)
        self.assertTrue(allowed("owner", "GET", "/finance/"))

    def test_tech_reaches_jobs_only(self):
        for path in ("/ro/", "/ro/4", "/ro/board", "/appointments/calendar", "/settings", "/vehicles/2"):
            self.assertTrue(allowed("tech", "GET", path), path)
        for path in ("/invoices/", "/customers/", "/estimates/", "/dashboard", "/ro/new", "/live/control", "/dispatch/1/decide"):
            self.assertFalse(allowed("tech", "GET", path), path)
        self.assertTrue(allowed("tech", "POST", "/ro/4/update-status"))
        for path in ("/ro/new", "/ro/4/delete-line-item/2", "/invoices/create/4", "/appointments/", "/dispatch/sending"):
            self.assertFalse(allowed("tech", "POST", path), path)

    def test_office_cannot_change_system_switches(self):
        self.assertTrue(allowed("office", "POST", "/dispatch/4/decide"))
        for path in ("/dispatch/sending", "/dispatch/policies", "/live/slots"):
            self.assertFalse(allowed("office", "POST", path), path)

    def test_unknown_pages_are_refused(self):
        self.assertFalse(allowed("office", "GET", "/something-new"))
        self.assertFalse(allowed("tech", "GET", "/something-new"))

    def test_customer_links_stay_public(self):
        self.assertTrue(allowed("tech", "GET", "/status/token"))
        self.assertTrue(allowed(None, "POST", "/sign/ro/token"))


class StaffLoginTests(unittest.TestCase):
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
        limiter.fails.clear() if hasattr(limiter, "fails") else None
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="crew", password_hash=hash_password(PASSWORD), role="tech"))
            db.commit()
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, username="owner", password=PASSWORD):
        client = TestClient(app, follow_redirects=False)
        return client, client.post("/login", data={"username": username, "password": password})

    def user(self, username):
        with self.Session() as db:
            return db.query(User).filter(User.username == username).first()

    def create(self, client, **changes):
        form = {"full_name": "New Hire", "username": "newhire", "password": "temporary-pass-1", "role": "tech", **changes}
        return client.post("/settings/users/create", data=form)

    def test_owner_creates_login_and_first_sign_in_forces_new_password(self):
        owner, _ = self.sign_in()
        self.assertIn("user_saved=created", self.create(owner).headers["location"])
        hire, response = self.sign_in("newhire", "temporary-pass-1")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(hire.get("/ro/").headers["location"], "/settings?change=1")
        short = hire.post("/settings/change-password", data={"current_password": "temporary-pass-1", "new_password": "short"})
        self.assertIn("at least 12", short.text)
        hire.post("/settings/change-password", data={"current_password": "temporary-pass-1", "new_password": OTHER})
        self.assertEqual(hire.get("/ro/").status_code, 200)

    def test_staff_cannot_create_logins(self):
        crew, _ = self.sign_in("crew")
        self.assertEqual(self.create(crew).status_code, 403)
        self.assertIsNone(self.user("newhire"))

    def test_bad_values_save_nothing(self):
        owner, _ = self.sign_in()
        for changes, code in (({"password": "short"}, "password"), ({"username": "a b"}, "username"), ({"role": "owner"}, "role"), ({"username": "crew"}, "taken")):
            self.assertIn(f"user_error={code}", self.create(owner, **changes).headers["location"])
        self.assertIsNone(self.user("newhire"))

    def test_turned_off_login_ends_now_and_cannot_sign_in(self):
        crew, _ = self.sign_in("crew")
        owner, _ = self.sign_in()
        owner.post("/settings/users/2/delete")
        self.assertEqual(crew.get("/ro/").headers["location"], "/login")
        _, response = self.sign_in("crew")
        self.assertIn("Invalid username or password", response.text)
        self.assertIsNotNone(self.user("crew"))
        owner.post("/settings/users/2/activate")
        self.assertEqual(self.sign_in("crew")[1].status_code, 302)

    def test_owner_login_cannot_be_turned_off_or_reset(self):
        owner, _ = self.sign_in()
        self.assertIn("user_error=missing", owner.post("/settings/users/1/delete").headers["location"])
        self.assertIn("user_error=missing", owner.post("/settings/users/1/reset-password", data={"new_password": OTHER}).headers["location"])
        self.assertTrue(self.user("owner").is_active)

    def test_owner_stays_signed_in_after_changing_password(self):
        owner, _ = self.sign_in()
        owner.post("/settings/change-password", data={"current_password": PASSWORD, "new_password": OTHER})
        self.assertEqual(owner.get("/settings").status_code, 200)

    def test_read_only_still_allows_logins_but_shows_a_plain_page(self):
        app.state.read_only = True
        owner, _ = self.sign_in()
        self.assertIn("user_saved=created", self.create(owner).headers["location"])
        blocked = owner.post("/customers/new", data={}, headers={"accept": "text/html"})
        self.assertEqual(blocked.status_code, 503)
        self.assertIn("nothing was changed", blocked.text)

    def test_blocked_page_is_plain_language(self):
        crew, _ = self.sign_in("crew")
        response = crew.get("/finance/", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Not available on your login", response.text)


if __name__ == "__main__":
    unittest.main()
