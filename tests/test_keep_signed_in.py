"""Keep me signed in on the staff login. Made-up data only."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient

from app.main import app
from app.routes import account_security
from test_security_center import PASSWORD, _Setup

DAY = 86400


class KeepSignedInTests(_Setup):
    def sign_in(self, name="owner", remember=False):
        client = TestClient(app, follow_redirects=False)
        data = {"username": name, "password": PASSWORD}
        if remember:
            data["remember"] = "on"
        response = client.post("/login", data=data)
        self.assertIn(response.status_code, (302, 303))
        return client, response

    def signed_in_at(self, client, seconds_later):
        with patch("time.time", return_value=time.time() + seconds_later):
            return client.get("/settings").status_code == 200

    def test_login_page_offers_the_checkbox(self):
        page = TestClient(app).get("/login")
        self.assertIn('name="remember"', page.text)
        self.assertIn("Keep me signed in", page.text)

    def test_without_the_box_the_security_setting_still_signs_you_out(self):
        client, _ = self.sign_in()
        self.assertTrue(self.signed_in_at(client, 6 * DAY))
        client, _ = self.sign_in()
        self.assertFalse(self.signed_in_at(client, 8 * DAY))  # default 168 hours

    def test_kept_sign_in_lasts_30_days_on_that_device(self):
        client, response = self.sign_in(remember=True)
        cookie = response.headers["set-cookie"].lower()
        self.assertIn(f"max-age={30 * DAY}", cookie)
        self.assertIn("httponly", cookie)
        self.assertTrue(self.signed_in_at(client, 8 * DAY))
        client, _ = self.sign_in(remember=True)
        self.assertTrue(self.signed_in_at(client, 29 * DAY))
        client, _ = self.sign_in(remember=True)
        self.assertFalse(self.signed_in_at(client, 31 * DAY))

    def test_kept_sign_in_beats_a_shorter_security_setting(self):
        client, _ = self.sign_in()
        self.save(client, session_hours="1")
        kept, _ = self.sign_in(remember=True)
        self.assertTrue(self.signed_in_at(kept, 2 * 3600))
        plain, _ = self.sign_in()
        self.assertFalse(self.signed_in_at(plain, 2 * 3600))

    def test_sign_out_ends_a_kept_sign_in(self):
        client, _ = self.sign_in(remember=True)
        self.assertEqual(client.get("/settings").status_code, 200)
        client.get("/logout")
        self.assertEqual(client.get("/settings").status_code, 302)

    def test_two_step_code_carries_the_choice(self):
        fake = SimpleNamespace(secret_encrypted="x", last_counter=0, confirmed_at=1)
        with patch.object(account_security, "confirmed_mfa", return_value=fake), \
             patch.object(account_security, "decrypt_secret", return_value="secret"), \
             patch.object(account_security, "check_code", return_value=5):
            client, response = self.sign_in(remember=True)
            self.assertEqual(response.headers["location"], "/login/verify")
            done = client.post("/login/verify", data={"code": "123456"})
            self.assertEqual(done.status_code, 303)
        self.assertTrue(self.signed_in_at(client, 20 * DAY))


if __name__ == "__main__":
    unittest.main()
