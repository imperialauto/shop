"""Home's job list must be usable on phones and by keyboard."""
from pathlib import Path
import unittest

from jinja2 import Environment


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/templates/dashboard.html"
BASE = ROOT / "app/templates/base.html"


class DashboardMobileROTests(unittest.TestCase):
    def test_job_rows_are_links_with_every_fact(self):
        # Home's job list is board-style rows, not a table: each row is one link a keyboard or phone can open.
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn('<a class="h-row" href="/ro/{{ ro.id }}" aria-label="Open job {{ ro.id }}">', text)
        for fact in ("ro.vehicle.label", "ro.customer.first_name", "Next step:", "ro.status.replace"):
            self.assertIn(fact, text)
        self.assertIn("@media (max-width: 900px)", text)  # columns stack on a phone

    def test_mobile_breakpoint_uses_table_card_labels(self):
        css = BASE.read_text(encoding="utf-8")
        self.assertIn('@media (max-width: 767px)', css)
        self.assertIn('td[data-label]::before', css)
        self.assertIn("content: attr(data-label)", css)

    def test_jinja_template_still_compiles(self):
        Environment().parse(TEMPLATE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
