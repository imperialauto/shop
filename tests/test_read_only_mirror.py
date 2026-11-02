"""Read-only mode must keep the Quo mirror working and everything else blocked.

Uses a mocked session so it creates no tables: test_recovery asserts an empty
fixture database at startup, and this module runs first.
"""
import unittest
from unittest.mock import MagicMock, patch

import fixture_environment  # noqa: F401  selects the disposable database first
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.routes import internal_messaging

MIRROR = "/internal/messaging/communications"
KEY = "fixture-internal-key"
MESSAGE = {"phone": "+16025550100", "direction": "inbound", "body": "Test", "externalId": "quo-1"}


class ReadOnlyMirrorTests(unittest.TestCase):
    def setUp(self):
        self.original_read_only = app.state.read_only
        app.state.read_only = True
        app.dependency_overrides[get_db] = lambda: MagicMock()
        patcher = patch.object(internal_messaging, "INTERNAL_API_KEY", KEY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)

    def tearDown(self):
        app.state.read_only = self.original_read_only
        app.dependency_overrides.pop(get_db, None)

    def test_valid_key_reaches_the_mirror(self):
        with patch.object(internal_messaging, "_find_customer_by_phone", return_value=None):
            response = self.client.post(MIRROR, json=MESSAGE, headers={"X-Internal-Key": KEY})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"logged": False, "reason": "no matching customer"})

    def test_wrong_or_missing_key_stays_blocked(self):
        self.assertEqual(self.client.post(MIRROR, json=MESSAGE).status_code, 503)
        response = self.client.post(MIRROR, json=MESSAGE, headers={"X-Internal-Key": "wrong"})
        self.assertEqual(response.status_code, 503)

    def test_unset_service_key_never_matches(self):
        with patch.object(internal_messaging, "INTERNAL_API_KEY", ""):
            response = self.client.post(MIRROR, json=MESSAGE, headers={"X-Internal-Key": ""})
        self.assertEqual(response.status_code, 503)

    def test_valid_key_does_not_open_other_writes(self):
        headers = {"X-Internal-Key": KEY}
        for path in (
            "/internal/messaging/repair-orders/1/signing-link",
            "/webhooks/openphone",
            "/customers/new",
            "/appointments/",
        ):
            self.assertEqual(self.client.post(path, json={}, headers=headers).status_code, 503, path)

    def test_only_post_is_exempt(self):
        response = self.client.put(MIRROR, json=MESSAGE, headers={"X-Internal-Key": KEY})
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
