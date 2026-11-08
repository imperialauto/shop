"""Owner business settings, on made-up values in a private database."""
import os
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import User
from app.public_site import bookable_service, booking_closed_days, mobile_zone
from app.routes.appointments import configured_hours, shop_buffer_minutes

PASSWORD = "fixture-password-1"


def form(**changes):
    data = {"buffer": "60", "lead": "120", "zones": "85307, 25, 20"}
    for i in range(6):
        data.update({f"open_{i}": "on", f"opens_{i}": "11:00", f"closes_{i}": "19:00", f"online_{i}": "on"})
    data["closes_5"], data["opens_5"] = "17:00", "12:00"
    for slug in ("diagnostic", "oil-service", "brake-inspection", "ac-check", "battery-charging", "pre-purchase"):
        data.update({f"price_{slug}": "", f"minutes_{slug}": "60", f"type_{slug}": "exact"})
    data.update({"price_diagnostic": "99.50", "mobile_battery-charging": "on", "minutes_pre-purchase": "90", "type_pre-purchase": "starting", "price_pre-purchase": "150"})
    data.update(changes)
    return data


class BusinessSettingsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.patch = patch.object(store, "SessionLocal", self.Session)
        self.patch.start()
        store.clear_cache()
        self.read_only = app.state.read_only
        app.state.read_only = True  # production is read-only, and settings must still save
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.add(User(id=3, username="boss", password_hash=hash_password(PASSWORD), role="admin"))
            db.commit()

    def tearDown(self):
        self.patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def test_owner_saves_and_the_app_uses_the_values(self):
        owner = self.sign_in()
        self.assertEqual(owner.get("/settings/business").status_code, 200)
        response = owner.post("/settings/business", data=form())
        self.assertEqual(response.headers["location"], "/settings/business?saved=1")
        with patch.dict(os.environ, {"SHOP_HOURS_JSON": "{}", "PUBLIC_SERVICE_CATALOG": "{}"}):
            hours = configured_hours()
            self.assertEqual(str(hours[5][0]), "12:00:00")
            self.assertNotIn(6, hours)
            self.assertEqual(bookable_service("diagnostic").price_label, "$99.50")
            self.assertEqual(bookable_service("pre-purchase").price_label, "From $150")
            self.assertIsNone(bookable_service("ac-check"))
            self.assertTrue(bookable_service("battery-charging") is None)  # mobile but no price yet
            self.assertEqual(mobile_zone("85307").fee, 25)
            self.assertEqual(shop_buffer_minutes(), 60)
            self.assertEqual(booking_closed_days(), {6})

    def test_every_change_is_recorded_with_who(self):
        owner = self.sign_in()
        owner.post("/settings/business", data=form())
        owner.post("/settings/business", data=form(price_diagnostic="105"))
        with self.Session() as db:
            changes = db.query(store.BusinessSettingChange).filter_by(key="booking_services").all()
            self.assertEqual(len(changes), 2)
            self.assertEqual({c.changed_by for c in changes}, {"owner"})
            self.assertIn('"price": 105', changes[-1].new_value)
        self.assertEqual(owner.post("/settings/business", data=form(price_diagnostic="105")).headers["location"], "/settings/business?saved=0")

    def test_bad_values_save_nothing(self):
        owner = self.sign_in()
        for changes, words in (({"closes_0": "10:00"}, "Monday"), ({"price_diagnostic": "abc"}, "price"),
                               ({"zones": "85307 25"}, "ZIP line 1"), ({"minutes_oil-service": "5"}, "length")):
            response = owner.post("/settings/business", data=form(**changes))
            self.assertEqual(response.status_code, 422)
            self.assertIn(words, response.text)
            self.assertIn("Nothing was saved", response.text)
        with self.Session() as db:
            self.assertEqual(db.query(store.BusinessSetting).count(), 0)

    def test_only_the_owner_can_open_or_save(self):
        for name in ("office", "boss"):
            client = self.sign_in(name)
            self.assertEqual(client.get("/settings/business").status_code, 403)
            self.assertEqual(client.post("/settings/business", data=form()).status_code, 403)
        with self.Session() as db:
            self.assertEqual(db.query(store.BusinessSetting).count(), 0)

    def test_railway_variables_still_work_with_nothing_saved(self):
        with patch.dict(os.environ, {"SHOP_BUFFER_MINUTES": "45"}):
            self.assertEqual(shop_buffer_minutes(), 45)

    def test_missing_table_falls_back_to_variables(self):
        def broken():
            raise RuntimeError("no table")
        store.clear_cache()
        with patch.object(store, "SessionLocal", broken), patch.dict(os.environ, {"SHOP_BUFFER_MINUTES": "30"}):
            self.assertEqual(store.raw("shop_buffer_minutes"), "30")


if __name__ == "__main__":
    unittest.main()
