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

    def test_signed_out_browser_goes_to_sign_in_then_back(self):
        client = TestClient(app, follow_redirects=False)
        page = client.get("/settings/business", headers={"accept": "text/html"})
        self.assertEqual((page.status_code, page.headers["location"]), (303, "/login"))
        self.assertEqual(client.post("/login", data={"username": "owner", "password": PASSWORD}).headers["location"], "/settings/business")
        self.assertEqual(client.post("/login", data={"username": "owner", "password": PASSWORD}).headers["location"], "/dashboard")

    def test_signed_out_api_call_still_gets_401(self):
        self.assertEqual(TestClient(app).get("/appointments/").status_code, 401)

    def test_only_local_pages_are_returned_to(self):
        from types import SimpleNamespace
        from app.auth import after_login
        for bad in ("//evil.example", "https://evil.example", "/\\evil"):
            self.assertEqual(after_login(SimpleNamespace(session={"next": bad}), "/dashboard"), "/dashboard")

    def make_job(self, tech_id=None):
        from app.models import Customer, RepairOrder, Vehicle
        with self.Session() as db:
            customer = Customer(first_name="Pat", last_name="Sample", phone="6025550100")
            db.add(customer)
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2016, make="Ford", model="Focus")
            db.add(vehicle)
            db.flush()
            order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="intake", concern="Made-up concern", assigned_tech_id=tech_id)
            db.add(order)
            db.commit()
            return order.id

    def test_tech_lands_on_my_day_with_their_jobs(self):
        mine, other = self.make_job(tech_id=2), self.make_job()
        crew, response = self.sign_in("crew")
        self.assertEqual(response.headers["location"], "/my-day")
        self.assertEqual(crew.get("/dashboard").headers["location"], "/my-day")
        page = crew.get("/my-day").text
        self.assertIn(f'/ro/{mine}#li-description', page)
        self.assertIn("Add the repairs and prices", page)
        # My Day must not show eight unrelated status buttons for an unpriced job.
        self.assertNotIn(f'/ro/{mine}/update-status?back=my-day', page)
        self.assertIn("Not assigned yet (1)", page)
        moved = crew.post(f"/ro/{mine}/update-status?back=my-day", data={"status": "in_progress"})
        self.assertEqual(moved.headers["location"], "/my-day")

    def test_my_day_uses_single_next_step_after_approval(self):
        from app.models import LineItem
        job = self.make_job(tech_id=2)
        with self.Session() as db:
            db.add(LineItem(repair_order_id=job, description="Approved repair",
                            item_type="labor", quantity=1, unit_price=150,
                            approval="approved"))
            db.commit()
        crew, _ = self.sign_in("crew")
        page = crew.get("/my-day").text
        self.assertIn("Approved. Start the work", page)
        self.assertIn(f'action="/ro/{job}/update-status?back=my-day"', page)
        self.assertEqual(page.count(f'action="/ro/{job}/update-status?back=my-day"'), 1)
        self.assertIn('name="status" value="in_progress"', page)
        self.assertNotIn('name="status" value="complete"', page)
        self.assertNotIn('name="status" value="waiting_approval"', page)

    def test_my_day_appointment_is_on_job_card_not_repeated(self):
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        from app.appointment_model import Appointment
        job = self.make_job(tech_id=2)
        other = self.make_job()
        phoenix_now = datetime.now(ZoneInfo("America/Phoenix"))
        at = phoenix_now.replace(hour=14, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
        with self.Session() as db:
            for idx, ro_id in enumerate((job, other)):
                db.add(Appointment(repair_order_id=ro_id, resource_id="shop",
                                   starts_at=at + timedelta(hours=idx),
                                   ends_at=at + timedelta(hours=idx + 1),
                                   status="confirmed", location="shop"))
            db.commit()
        crew, _ = self.sign_in("crew")
        page = crew.get("/my-day").text
        self.assertIn("2 total appointments today", page)
        self.assertIn("Additional appointments (0)", page)
        self.assertIn("Today 2:30 PM", page)
        self.assertIn("Today 3:30 PM", page)
        self.assertIn(f'/ro/{job}', page)
        self.assertIn(f'/ro/{other}', page)

    def test_my_day_hides_finished_work_and_none_labels(self):
        from app.models import RepairOrder, Vehicle
        done, blank = self.make_job(), self.make_job()
        with self.Session() as db:
            db.get(RepairOrder, done).status = "complete"
            vehicle = db.get(Vehicle, db.get(RepairOrder, blank).vehicle_id)
            vehicle.year, vehicle.make, vehicle.model = None, "Unknown", "Unknown"
            db.commit()
        crew, _ = self.sign_in("crew")
        page = crew.get("/my-day").text
        self.assertIn("Not assigned yet (1)", page)
        self.assertNotIn("Unknown Unknown", page)
        self.assertNotIn("Take it", page)  # techs don't assign themselves

    def test_status_must_be_real_and_tech_cannot_deliver(self):
        job = self.make_job(tech_id=2)
        crew, _ = self.sign_in("crew")
        self.assertEqual(crew.post(f"/ro/{job}/update-status", data={"status": "made-up"}).status_code, 422)
        self.assertEqual(crew.post(f"/ro/{job}/update-status", data={"status": "delivered"}).status_code, 403)
        owner, _ = self.sign_in()
        self.assertEqual(owner.post(f"/ro/{job}/update-status", data={"status": "delivered"}).status_code, 302)

    def test_owner_assigns_and_tech_cannot(self):
        job = self.make_job()
        crew, _ = self.sign_in("crew")
        self.assertEqual(crew.post(f"/ro/{job}/assign", data={"assigned_tech_id": "2"}).status_code, 403)
        owner, _ = self.sign_in()
        self.assertEqual(owner.post(f"/ro/{job}/assign", data={"assigned_tech_id": "2"}).status_code, 302)
        from app.models import RepairOrder
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, job).assigned_tech_id, 2)
        self.assertEqual(owner.post(f"/ro/{job}/assign", data={"assigned_tech_id": "999"}).status_code, 422)

    def test_blocked_page_is_plain_language(self):
        crew, _ = self.sign_in("crew")
        response = crew.get("/finance/", headers={"accept": "text/html"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("Not available on your login", response.text)


if __name__ == "__main__":
    unittest.main()
