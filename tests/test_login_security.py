"""Two-step login, failed-login limits, and cross-site protection."""
import time
import unittest
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import User
from app.security import LoginLimiter, STEP_SECONDS, UserMfa, check_code, code_at, decrypt_secret, encrypt_secret, limiter, new_secret

PASSWORD = "made-up-password-only"


def current_code(secret, offset=0):
    return code_at(secret, int(time.time() // STEP_SECONDS) + offset)


class CodeTests(unittest.TestCase):
    def test_matches_the_published_rfc_6238_vector(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(code_at(secret, 59 // 30), "287082")

    def test_code_cannot_be_reused(self):
        secret = new_secret()
        step = check_code(secret, current_code(secret), 0)
        self.assertIsNotNone(step)
        self.assertIsNone(check_code(secret, code_at(secret, step), step))

    def test_wrong_and_old_codes_fail(self):
        secret = new_secret()
        self.assertIsNone(check_code(secret, "abc", 0))
        self.assertIsNone(check_code(secret, current_code(secret, -5), 0))

    def test_secret_is_encrypted(self):
        secret = new_secret()
        stored = encrypt_secret(secret)
        self.assertNotIn(secret, stored)
        self.assertEqual(decrypt_secret(stored), secret)
        self.assertIsNone(decrypt_secret("tampered"))

    def test_limiter_locks_and_expires(self):
        limits = LoginLimiter()
        for _ in range(5):
            limits.record("a", now=100)
        self.assertTrue(limits.locked("a", now=101))
        self.assertFalse(limits.locked("a", now=100 + 16 * 60))


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add(User(id=1, username="sample-owner", password_hash=hash_password(PASSWORD), role="admin"))
            db.commit()

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.addCleanup(self.engine.dispose)
        limiter.failures.clear()
        self.client = TestClient(app, follow_redirects=False)

    def login(self, password=PASSWORD):
        return self.client.post("/login", data={"username": "sample-owner", "password": password})

    def enroll(self):
        self.login()
        self.client.post("/settings/security/start")
        with self.Session() as db:
            secret = decrypt_secret(db.get(UserMfa, 1).secret_encrypted)
        page = self.client.post("/settings/security/confirm", data={"code": current_code(secret, -1)})
        self.client.get("/logout")
        return secret, page.text

    def test_password_alone_logs_in_until_enrolled(self):
        self.assertEqual(self.login().headers["location"], "/dashboard")

    def test_enrolled_login_needs_a_code(self):
        secret, _ = self.enroll()
        self.assertEqual(self.login().headers["location"], "/login/verify")
        self.assertEqual(self.client.get("/settings").status_code, 302)
        self.assertEqual(self.client.post("/login/verify", data={"code": "000000"}).status_code, 401)
        self.assertEqual(self.client.post("/login/verify", data={"code": current_code(secret)}).headers["location"], "/dashboard")
        self.assertEqual(self.client.get("/settings").status_code, 200)

    def test_recovery_code_works_once(self):
        _, page = self.enroll()
        code = page.split("<pre", 1)[1].split(">", 1)[1].split()[0]
        self.login()
        self.assertEqual(self.client.post("/login/verify", data={"code": code}).status_code, 303)
        self.client.get("/logout")
        self.login()
        self.assertEqual(self.client.post("/login/verify", data={"code": code}).status_code, 401)

    def test_wrong_code_at_setup_leaves_it_off(self):
        self.login()
        self.client.post("/settings/security/start")
        self.assertIn("didn't match", self.client.post("/settings/security/confirm", data={"code": "000000"}).text)
        with self.Session() as db:
            self.assertIsNone(db.get(UserMfa, 1).confirmed_at)

    def test_turning_off_needs_password_and_code(self):
        secret, _ = self.enroll()
        self.login()
        self.client.post("/login/verify", data={"code": current_code(secret)})
        wrong = self.client.post("/settings/security/disable", data={"password": "wrong", "code": current_code(secret, 1)})
        self.assertIn("stays on", wrong.text)
        self.client.post("/settings/security/disable", data={"password": PASSWORD, "code": current_code(secret, 1)})
        with self.Session() as db:
            self.assertIsNone(db.get(UserMfa, 1))

    def test_repeated_wrong_passwords_lock_the_login(self):
        for _ in range(5):
            self.login("wrong")
        self.assertEqual(self.login().status_code, 429)

    def test_pending_login_expires(self):
        self.enroll()
        self.login()
        with patch("app.routes.account_security.time.time", return_value=time.time() + 600):
            self.assertEqual(self.client.get("/login/verify").headers["location"], "/login")

    def test_other_sites_cannot_post(self):
        self.login()
        blocked = self.client.post("/settings/security/start", headers={"origin": "https://example.com"})
        self.assertEqual(blocked.status_code, 403)
        same = self.client.post("/settings/security/start", headers={"origin": "http://testserver"})
        self.assertEqual(same.status_code, 303)

    def test_security_headers(self):
        headers = self.client.get("/login").headers
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
