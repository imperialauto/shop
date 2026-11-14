"""Old Squarespace links land on the right page of the new site (made-up requests only)."""
import unittest

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient

from app.legacy_redirects import LEGACY_PATHS
from app.main import app


class LegacyRedirects(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_every_old_page_redirects_permanently(self):
        for old, new in LEGACY_PATHS.items():
            for path in (old, old + "/"):
                with self.subTest(path=path):
                    res = self.client.get(path, follow_redirects=False)
                    self.assertEqual(res.status_code, 301)
                    self.assertEqual(res.headers["location"], new)

    def test_targets_are_real_pages(self):
        for new in set(LEGACY_PATHS.values()):
            with self.subTest(page=new):
                self.assertEqual(self.client.get(new).status_code, 200)

    def test_dps6_and_towing_keep_their_own_pages(self):
        self.assertEqual(LEGACY_PATHS["/ford-dps6-clutch-replacement-phoenix"], "/site/services/dps6-transmission")
        self.assertEqual(LEGACY_PATHS["/towing"], "/site/services/towing")


if __name__ == "__main__":
    unittest.main()
