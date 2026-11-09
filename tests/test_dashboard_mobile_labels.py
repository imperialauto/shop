"""Dashboard open-RO list must be usable on phones and by keyboard."""
from pathlib import Path
import unittest

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/templates/dashboard.html"
BASE = ROOT / "app/templates/base.html"


class DashboardMobileROTests(unittest.TestCase):
    def test_mobile_table_has_all_accessible_column_labels(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        for column in ("RO", "Customer", "Vehicle", "Next step", "Status", "Date"):
            self.assertIn(f'data-label="{column}"', text)
        self.assertIn('href="/ro/{{ ro.id }}" aria-label="Open repair order {{ ro.id }}"', text)

    def test_mobile_breakpoint_uses_table_card_labels(self):
        css = BASE.read_text(encoding="utf-8")
        self.assertIn('@media (max-width: 767px)', css)
        self.assertIn('td[data-label]::before', css)
        self.assertIn("content: attr(data-label)", css)

    def test_jinja_template_still_compiles(self):
        Environment().parse(TEMPLATE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
