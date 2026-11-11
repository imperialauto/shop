"""Rental-page QA: the first action must start an online request, not dead-end."""
import unittest

import fixture_environment  # noqa: F401 -- disposable fixture settings only
from fastapi.testclient import TestClient

from app.main import app


class RentalsActionTests(unittest.TestCase):
    def test_rentals_page_has_online_request_primary_action(self):
        with TestClient(app) as client:
            page = client.get("/site/rentals", follow_redirects=False)
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/site/estimate?service=rentals"', page.text)
        self.assertIn("Request online", page.text)
        # The customer can initiate without a phone call; the form still stays
        # disabled in production until its separate launch acceptance.
        self.assertNotIn('class="btn btn-gold" href="tel:', page.text)

    def test_rental_details_page_and_rentals_navigation_have_same_action(self):
        with TestClient(app) as client:
            detail = client.get("/site/services/rentals")
            shortcut = client.get("/site/rentals")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(shortcut.status_code, 200)
        action = 'href="/site/estimate?service=rentals"'
        self.assertIn(action, detail.text)
        self.assertIn(action, shortcut.text)


if __name__ == "__main__":
    unittest.main()
