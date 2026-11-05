"""Owner claim tests: token, CSRF, one-time semantics, and recovery mode.

This uses a mocked DB and never touches production, alters the fixture schema,
or creates a real default owner password.
"""
import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fixture_environment  # noqa: F401 -- select disposable test DB first
from fastapi.testclient import TestClient
from fastapi import HTTPException

from app.database import get_db
from app.main import app
from app.auth import require_admin, get_current_user, login_user
from app.routes import owner_setup

TOKEN = "railway-only-test-token-not-a-real-secret-123456789"


class OwnerSetupTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.client = TestClient(app)
        app.dependency_overrides[get_db] = lambda: self.db
        self.variable = patch.dict(os.environ, {"IMPERIAL_OWNER_SETUP_TOKEN": TOKEN})
        self.variable.start()

    def tearDown(self):
        self.variable.stop()
        app.dependency_overrides.pop(get_db, None)

    def get_form_token(self, user):
        with patch.object(owner_setup, "existing_unclaimed_user", return_value=user):
            result = self.client.get("/owner-setup")
        self.assertEqual(result.status_code, 200)
        match = re.search(r'name="csrf" value="([^"]+)"', result.text)
        self.assertIsNotNone(match)
        return match.group(1)

    def test_owner_setup_get_is_read_only(self):
        user = SimpleNamespace(id=1, role="admin")
        self.get_form_token(user)
        self.db.commit.assert_not_called()
        self.db.add.assert_not_called()

    def test_claim_requires_correct_private_token(self):
        user = SimpleNamespace(id=1, role="admin", username="old", password_hash="oldhash")
        csrf = self.get_form_token(user)
        with patch.object(owner_setup, "existing_unclaimed_user", return_value=user):
            result = self.client.post("/owner-setup", data={
                "setup_token": "invalid",
                "csrf": csrf,
                "username": "owner",
                "password": "a-long-unique-passphrase-2026",
                "confirm_password": "a-long-unique-passphrase-2026",
            })
        self.assertEqual(result.status_code, 403)
        self.db.commit.assert_not_called()

    def test_claim_sets_owner_on_existing_user_once(self):
        user = SimpleNamespace(id=1, role="admin", username="old", password_hash="oldhash")
        csrf = self.get_form_token(user)
        with patch.object(owner_setup, "existing_unclaimed_user", side_effect=[user, None]), patch.object(owner_setup, "hash_password", return_value="hashed-secret"):
            response = self.client.post("/owner-setup", data={
                "setup_token": TOKEN,
                "csrf": csrf,
                "username": "imperial-owner",
                "password": "a-long-unique-passphrase-2026",
                "confirm_password": "a-long-unique-passphrase-2026",
            }, follow_redirects=False)
            subsequent = self.client.get("/owner-setup")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location"), "/finance/")
        self.assertEqual(user.role, "owner")
        self.assertEqual(user.username, "imperial-owner")
        self.assertEqual(user.password_hash, "hashed-secret")
        self.assertEqual(subsequent.status_code, 410)
        self.db.commit.assert_called_once()
        self.db.add.assert_not_called()

    def test_setup_disabled_without_railway_variable(self):
        os.environ.pop("IMPERIAL_OWNER_SETUP_TOKEN", None)
        response = self.client.get("/owner-setup")
        self.assertEqual(response.status_code, 404)

    def test_old_sessions_are_invalidated_by_owner_password_reset(self):
        user = SimpleNamespace(id=1, role="owner", username="owner", password_hash="oldhash")
        req = SimpleNamespace(session={})
        login_user(req, user)
        self.db.query.return_value.filter.return_value.first.return_value = user
        self.assertIs(get_current_user(req, self.db), user)
        user.password_hash = "newhash"
        with self.assertRaises(HTTPException) as context:
            get_current_user(req, self.db)
        self.assertEqual(context.exception.status_code, 401)

    def test_owner_has_admin_access(self):
        require_admin(SimpleNamespace(role="owner"))


if __name__ == "__main__":
    unittest.main()
