"""The arithmetic behind the money overview, kept free of web code so it can be tested on its own.

Business and personal money never mix: every total here is for one scope.
"""
import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.finance_model import BILL_CATEGORIES, REPEATS, SCOPES

CLOSED_WEEKDAYS = {6}  # Sunday
DEFAULT_DAILY_GOAL = 500
WINDOWS = (7, 14, 30)


def cents(value) -> int:
    """'$1,977.39' or '-214.05' or 1977.39 -> whole cents. Raises ValueError on anything else."""
    text = str(value).strip().replace("$", "").replace(",", "").replace(" ", "")
    if not re.fullmatch(r"-?\d+(\.\d{1,2})?", text):
        raise ValueError(f"'{value}' isn't an amount")
    sign = -1 if text.startswith("-") else 1
    whole, _, frac = text.lstrip("-").partition(".")
    return sign * (int(whole) * 100 + int((frac + "00")[:2]))


def dollars(amount_cents: int) -> float:
    return round(amount_cents / 100, 2)


def parse_date(text: str, today: date) -> date:
    """2026-12-04, 12/4/2026, 12/4/26 or 12/4 (this year, or next year when that date is long gone)."""
    text = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?", text)
    if not match:
        raise ValueError(f"'{text}' isn't a date (try 12/4 or 2026-12-04)")
    month, day, year = int(match[1]), int(match[2]), match[3]
    if year:
        found = date(int(year) + (2000 if len(year) == 2 else 0), month, day)
    else:
        found = date(today.year, month, day)
        if (today - found).days > 120:
            found = date(today.year + 1, month, day)
    return found


def next_due(due: date, repeats: str) -> date | None:
    if repeats == "weekly":
        return due + timedelta(days=7)
    if repeats == "monthly":
        year, month = (due.year + 1, 1) if due.month == 12 else (due.year, due.month + 1)
        return date(year, month, min(due.day, calendar.monthrange(year, month)[1]))
    return None


def working_days(start: date, end: date) -> int:
    """Open days from start to end, both included. The shop is closed Sundays."""
    days, current = 0, start
    while current <= end:
        if current.weekday() not in CLOSED_WEEKDAYS:
            days += 1
        current += timedelta(days=1)
    return days


@dataclass
class ScopeTotals:
    cash: int = 0           # spendable: balances minus money held for customers
    held: int = 0
    overdue: int = 0
    due_in_window: int = 0
    expected_in: int = 0
    monthly_costs: int = 0
    overdue_bills: list = field(default_factory=list)
    window_bills: list = field(default_factory=list)
    later_bills: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    incoming: list = field(default_factory=list)

    @property
    def owed(self) -> int:
        return self.overdue + self.due_in_window

    @property
    def short(self) -> int:
        """How much more has to come in by the end of the window. Zero when covered."""
        return max(self.owed - self.cash - self.expected_in, 0)


def totals(accounts, bills, incoming, today: date, window_days: int) -> dict:
    end = today + timedelta(days=window_days - 1)
    out = {scope: ScopeTotals() for scope in SCOPES}
    for account in accounts:
        if not account.active:
            continue
        t = out[account.scope]
        t.accounts.append(account)
        t.cash += account.balance_cents - account.held_cents
        t.held += account.held_cents
    for bill in sorted(bills, key=lambda b: (b.due_date, b.name)):
        if bill.status != "open":
            continue
        t = out[bill.scope]
        if bill.due_date < today:
            t.overdue += bill.amount_cents
            t.overdue_bills.append(bill)
        elif bill.due_date <= end:
            t.due_in_window += bill.amount_cents
            t.window_bills.append(bill)
        else:
            t.later_bills.append(bill)
        if bill.repeats == "monthly":
            t.monthly_costs += bill.amount_cents
        elif bill.repeats == "weekly":
            t.monthly_costs += round(bill.amount_cents * 52 / 12)
    for item in sorted(incoming, key=lambda i: (i.expected_date is None, i.expected_date or today, i.source)):
        if item.status != "expected":
            continue
        t = out[item.scope]
        t.incoming.append(item)
        if item.expected_date is not None and item.expected_date <= end:
            t.expected_in += item.amount_cents
    return {"end": end, "days_open": working_days(today, end), **out}


def per_day(short_cents: int, days_open: int) -> int:
    return -(-short_cents // days_open) if days_open else short_cents


def spending_by_category(bills, scope: str, month_start: date) -> list[dict]:
    """This month's bills for one scope, paid or not, grouped by category, biggest first."""
    month_end = next_due(month_start, "monthly") - timedelta(days=1)
    groups: dict[str, int] = {}
    for bill in bills:
        if bill.scope != scope or bill.status == "skipped" or not (month_start <= bill.due_date <= month_end):
            continue
        groups[bill.category] = groups.get(bill.category, 0) + bill.amount_cents
    total = sum(groups.values())
    return [{"category": name, "cents": amount, "share": round(amount * 100 / total) if total else 0}
            for name, amount in sorted(groups.items(), key=lambda kv: -kv[1])]


# ---- pasted figures ----------------------------------------------------------

PASTE_HELP = (
    "balance | BofA business checking | 174.80 | business",
    "balance | Apple Cash | 413.55 | business | held 413.55",
    "bill | APS electric | 514.85 | 12/4 | business | utilities | monthly",
    "owed | Steve Cardona | 1,977.39 | 11/30",
    "paid | APS electric",
    "got | Steve Cardona",
)


@dataclass
class PasteLine:
    kind: str
    name: str
    amount: int | None = None
    when: date | None = None
    scope: str = "business"
    category: str = "other"
    repeats: str = "once"
    held: int = 0


def parse_paste(text: str, today: date) -> tuple[list[PasteLine], list[str]]:
    """One figure per line, fields split by '|'. Returns what was understood and one message per line that wasn't."""
    lines, problems = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.strip().startswith("#"):
            continue
        parts = [p.strip() for p in raw.split("|")]
        kind = parts[0].lower().rstrip(":")
        try:
            if kind in ("paid", "got") and len(parts) >= 2 and parts[1]:
                lines.append(PasteLine(kind, parts[1][:80]))
                continue
            if kind not in ("balance", "bill", "owed") or len(parts) < 3 or not parts[1]:
                raise ValueError("start with balance, bill, owed, paid or got, then the name and amount")
            line = PasteLine(kind, parts[1][:80], cents(parts[2]))
            rest = parts[3:]
            if kind in ("bill", "owed") and rest and rest[0] and rest[0].lower() not in SCOPES:
                line.when = parse_date(rest[0], today)
                rest = rest[1:]
            elif kind == "bill":
                raise ValueError("a bill needs a due date")
            for word in rest:
                low = word.lower()
                if not low:
                    continue
                if low in SCOPES:
                    line.scope = low
                elif kind == "bill" and low in BILL_CATEGORIES:
                    line.category = low
                elif kind == "bill" and low in REPEATS:
                    line.repeats = low
                elif kind == "balance" and low.startswith("held"):
                    line.held = cents(word[4:])
                else:
                    raise ValueError(f"didn't understand '{word}'")
            if kind != "balance" and line.amount <= 0:
                raise ValueError("the amount has to be more than zero")
            lines.append(line)
        except ValueError as exc:
            problems.append(f"Line {number}: {exc}.")
    return lines, problems
