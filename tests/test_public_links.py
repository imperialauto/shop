import os
import unittest
from unittest import mock

from app import approval_links, public_links


class PublicLinkTests(unittest.TestCase):
    def test_default_is_shop_domain(self):
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": ""}):
            self.assertEqual(public_links.public_url("/status/abc"), "https://imperialautophx.com/status/abc")

    def test_railway_address_is_never_used(self):
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://web-production-94989.up.railway.app/"}):
            self.assertEqual(public_links.base_url(), "https://imperialautophx.com")
            self.assertTrue(approval_links.base_url().startswith("https://imperialautophx.com"))

    def test_other_shop_address_is_kept(self):
        with mock.patch.dict(os.environ, {"PUBLIC_BASE_URL": "https://www.imperialautophx.com/"}):
            self.assertEqual(public_links.base_url(), "https://www.imperialautophx.com")

    def test_no_railway_host_left_in_app_code(self):
        root = os.path.join(os.path.dirname(__file__), "..", "app")
        hits = []
        for folder, _, files in os.walk(root):
            for name in files:
                if name.endswith((".py", ".html", ".js")) and name != "public_links.py":
                    with open(os.path.join(folder, name), encoding="utf-8") as fh:
                        if "up.railway.app" in fh.read():
                            hits.append(name)
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
