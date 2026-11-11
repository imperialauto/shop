"""AI-assist rate and HTML safety checks with synthetic records; no customer communication."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import fixture_environment  # noqa: F401
from app.routes import ai_assist


class FakeDB:
    def __init__(self, order):
        self.order = order

    def query(self, _model):
        return self

    def filter(self, *_args):
        return self

    def first(self):
        return self.order


def example_order():
    return SimpleNamespace(
        vehicle=SimpleNamespace(year=2013, make="Ford", model="Fusion", engine="2.0L"),
        mileage_in=120000,
        tech_notes="Fuel tank leak; my supplier cost is $81.83 (private).",
        concern="Fuel smell",
    )


class AIAssistGuardTests(unittest.TestCase):
    def test_draft_uses_approved_rates_and_never_echoes_model_html(self):
        with (
            patch.object(ai_assist, "require_user", return_value=object()),
            patch.object(ai_assist, "rates", return_value={
                "shop": 150.0, "mobile": 165.0, "diesel": 175.0, "cspr": 165.0,
            }),
            patch.object(ai_assist, "call_claude", return_value="<script>alert('x')</script>") as model,
        ):
            response = ai_assist.draft_line_items(None, 1, FakeDB(example_order()))
        prompt = model.call_args.args[0]
        self.assertIn("$150.00/hr", prompt)
        self.assertIn("$175.00/hr", prompt)
        self.assertIn("NO approved universal parts markup", prompt)
        self.assertIn("Vendor cost", prompt)
        self.assertIn("REVIEW REQUIRED", prompt)
        self.assertNotIn("25-30%", prompt)
        self.assertNotIn("Gas/other labor: $130", prompt)
        body = response.body.decode()
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_diagnostic_and_upsell_outputs_escape_model_html(self):
        for route in (ai_assist.suggest_diagnostic, ai_assist.suggest_upsells):
            with (
                patch.object(ai_assist, "require_user", return_value=object()),
                patch.object(ai_assist, "call_claude", return_value="<img src=x onerror=alert(1)>"),
            ):
                response = route(None, 1, FakeDB(example_order()))
            self.assertNotIn("<img", response.body.decode())
            self.assertIn("&lt;img", response.body.decode())


if __name__ == "__main__":
    unittest.main()
