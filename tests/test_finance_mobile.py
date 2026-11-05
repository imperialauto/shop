"""Finance route smoke tests — no production data or schema mutation."""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import fixture_environment  # noqa: F401 -- disposable sqlite before app imports
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.routes.finance import month_boundaries


class FinanceMobileViewTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.query.return_value.filter.return_value.one.side_effect = [
            (100.0, 2),
            (250.0, 1),
        ]
        self.db.query.return_value.options.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(get_db, None)

    def test_owner_can_view_mobile_finance_page_without_mutation(self):
        user = SimpleNamespace(role="admin", full_name="Owner", username="owner")
        with patch("app.routes.finance.get_current_user", return_value=user):
            response = self.client.get("/finance/?period=2026-11")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Financial Command Center", response.text)
        self.assertIn("100.00", response.text)
        self.assertIn("not audited", response.text.lower())
        self.db.commit.assert_not_called()
        self.db.add.assert_not_called()

    def test_empty_ledger_does_not_imply_zero_revenue(self):
        user = SimpleNamespace(role="admin", full_name="Owner", username="owner")
        self.db.query.return_value.filter.return_value.one.side_effect = [
            (0.0, 0),
            (0.0, 0),
        ]
        with patch("app.routes.finance.get_current_user", return_value=user):
            response = self.client.get("/finance/?period=2026-11")
        self.assertEqual(response.status_code, 200)
        self.assertIn("not $0 business revenue", response.text.lower())
        self.assertNotIn("Recorded payments · November 2026</div>", response.text)

    def test_service_advisor_cannot_view_financial_page(self):
        user = SimpleNamespace(role="tech", full_name="Advisor", username="advisor")
        with patch("app.routes.finance.get_current_user", return_value=user):
            response = self.client.get("/finance/")
        self.assertEqual(response.status_code, 403)

    def test_unauthenticated_request_redirects_to_login(self):
        with patch("app.routes.finance.get_current_user", side_effect=HTTPException(status_code=401)):
            response = self.client.get("/finance/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")

    def test_month_bounds_are_exclusive_and_phoenix_local(self):
        # Arizona is UTC-7 year-round. November and December start at 07:00 UTC.
        self.assertEqual(
            month_boundaries("2026-11"),
            (datetime(2026, 11, 1, 7), datetime(2026, 12, 1, 7)),
        )
        self.assertEqual(
            month_boundaries("2026-12"),
            (datetime(2026, 12, 1, 7), datetime(2027, 1, 1, 7)),
        )


if __name__ == "__main__":
    unittest.main()
