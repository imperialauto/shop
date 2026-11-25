import unittest
from datetime import datetime, timezone

import fixture_environment  # noqa: F401
from app.templating import shop_time


class ShopTimeTest(unittest.TestCase):
    def test_evening_in_phoenix_is_still_today(self):
        # 2:08 AM UTC on Nov 25 is 7:08 PM on Nov 24 at the shop.
        self.assertEqual(shop_time(datetime(2026, 11, 25, 2, 8)).strftime("%b %-d %-I:%M %p"), "Nov 24 7:08 PM")
        self.assertEqual(shop_time(datetime(2026, 11, 25, 2, 8, tzinfo=timezone.utc)).day, 24)

    def test_not_a_time_passes_through(self):
        self.assertIsNone(shop_time(None))


if __name__ == "__main__":
    unittest.main()
