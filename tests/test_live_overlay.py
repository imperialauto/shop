import os
import fixture_environment  # noqa: F401
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from app.main import app


class LiveOverlayAccessTest(unittest.TestCase):
    def test_overlay_is_closed_without_configured_token(self):
        with mock.patch.dict(os.environ, {"LIVE_OVERLAY_TOKEN": ""}), TestClient(app) as client:
            self.assertEqual(client.get("/live/overlay/anything").status_code, 404)
            self.assertEqual(client.get("/live/overlay/anything/data").status_code, 404)

    def test_wrong_token_is_rejected(self):
        with mock.patch.dict(os.environ, {"LIVE_OVERLAY_TOKEN": "right"}), TestClient(app) as client:
            self.assertEqual(client.get("/live/overlay/wrong/data").status_code, 404)

    def test_overlay_page_is_transparent_and_uncached(self):
        with mock.patch.dict(os.environ, {"LIVE_OVERLAY_TOKEN": "right"}), TestClient(app) as client:
            response = client.get("/live/overlay/right")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("background: transparent", response.text)

    def test_control_requires_login_and_writes_stay_paused(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/live/control", follow_redirects=False).status_code, 302)
            self.assertEqual(client.post("/live/slots", json={}).status_code, 503)


if __name__ == "__main__":
    unittest.main()
