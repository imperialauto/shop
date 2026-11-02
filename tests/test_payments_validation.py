"""Payment entry rules, checked without a database."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as Row

import fixture_environment  # noqa: F401
from app.routes.invoices import payment_error

NOW = datetime(2026, 11, 2, 12, 0)


def invoice(total=150.0, payments=(), status="sent"):
    return Row(total=total, status=status, payments=list(payments))


def paid(amount, method="cash", reference=None, age=timedelta(hours=1)):
    return Row(amount=amount, method=method, reference=reference, paid_at=NOW - age)


class PaymentValidationTests(unittest.TestCase):
    def test_exact_and_partial_payments_pass(self):
        self.assertIsNone(payment_error(invoice(), 150, "zelle", "", NOW))
        self.assertIsNone(payment_error(invoice(), 50.25, "cash", "", NOW))

    def test_typo_above_the_balance_is_refused(self):
        self.assertIn("$150.00 balance", payment_error(invoice(), 15000, "cash", "", NOW))
        self.assertIn("$50.00 balance", payment_error(invoice(payments=[paid(100)]), 75, "cash", "", NOW))

    def test_bad_amounts_are_refused(self):
        for amount in (0, -20, float("nan"), float("inf"), 10.005):
            self.assertIsNotNone(payment_error(invoice(), amount, "cash", "", NOW), amount)

    def test_unknown_method_and_void_invoice_are_refused(self):
        self.assertIsNotNone(payment_error(invoice(), 10, "cashapp", "", NOW))
        self.assertIn("void", payment_error(invoice(status="void"), 10, "cash", "", NOW))

    def test_paid_in_full_is_refused(self):
        self.assertIn("paid in full", payment_error(invoice(payments=[paid(150)]), 1, "cash", "", NOW))

    def test_duplicates_are_refused(self):
        repeat = invoice(payments=[paid(50, reference="Z123")])
        self.assertIn("reference", payment_error(repeat, 20, "zelle", "Z123", NOW))
        click = invoice(payments=[paid(50, age=timedelta(seconds=30))])
        self.assertIn("just recorded", payment_error(click, 50, "cash", "", NOW))
        self.assertIsNone(payment_error(invoice(payments=[paid(50)]), 50, "cash", "", NOW))
