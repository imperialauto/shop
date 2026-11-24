"""How customer texts and terms say they can pay.

The owner's rule, Nov 24: QuickBooks payments, Zelle and Apple Pay going forward (cash still taken).
QuickBooks is a card link the owner sends from QuickBooks, so texts offer it rather than include one.
"""
import os


def zelle_to() -> str:
    return os.getenv("DEPOSIT_ZELLE_TO", "jaelan@imperialautocarellc.com").strip()


def apple_pay_to() -> str:
    return os.getenv("APPLE_PAY_TO", "").strip()


def phrase() -> str:
    """The words after "by", e.g. "...a $200.00 deposit, half of the total, by <phrase>"."""
    apple = f"Apple Pay to {apple_pay_to()}" if apple_pay_to() else "Apple Pay"
    return f"Zelle to {zelle_to()}, {apple}, cash, or card with a QuickBooks link I can text you"


TERMS_LINE = ("We accept card through a QuickBooks payment link, Zelle, Apple Pay and cash. "
              "Financing may be available through Snap Finance.")
