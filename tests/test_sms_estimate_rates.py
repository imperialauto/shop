"""SMS estimates must use the shop's rate table, never a rate or markup the AI invents."""
import asyncio, json, os, unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_sms_estimate_rates.db")

from app.routes import webhooks


def fake_groq(payload):
    async def create(**_):
        msg = SimpleNamespace(content="Here you go: " + json.dumps(payload))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])
    return lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class SmsEstimateRatesTests(unittest.TestCase):
    def run_estimate(self, payload, **data):
        with patch.object(webhooks, "AsyncGroq", fake_groq(payload)):
            return asyncio.run(webhooks.generate_estimate({"year": 2013, "make": "Ford", "model": "Fusion", **data}))

    def test_ai_invented_rate_is_replaced_by_shop_rate(self):
        est = self.run_estimate({"labor_hours": 2, "labor_rate": 130, "labor_total": 260, "rate_type": "shop", "parts_low": 100, "parts_high": 120})
        self.assertEqual(est["labor_rate"], 150.0)
        self.assertEqual(est["labor_total"], 300.0)
        self.assertEqual((est["total_low"], est["total_high"]), (400.0, 420.0))

    def test_diesel_uses_diesel_rate(self):
        est = self.run_estimate({"labor_hours": 1, "rate_type": "diesel", "parts_low": 0, "parts_high": 0})
        self.assertEqual(est["labor_rate"], 175.0)

    def test_customer_supplied_parts_forces_cspr_and_zero_parts(self):
        est = self.run_estimate({"labor_hours": 2, "rate_type": "shop", "labor_rate": 145, "parts_low": 50, "parts_high": 80}, customer_supplied_parts=True)
        self.assertEqual(est["rate_type"], "cspr")
        self.assertEqual(est["labor_rate"], 165.0)
        self.assertEqual((est["parts_low"], est["parts_high"], est["total_high"]), (0.0, 0.0, 330.0))

    def test_prompt_no_longer_states_old_rates_or_markup(self):
        for stale in ("$130/hr", "$145/hr", "25-30%"):
            self.assertNotIn(stale, webhooks.ESTIMATE_SYSTEM)


if __name__ == "__main__":
    unittest.main()
