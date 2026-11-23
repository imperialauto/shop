"""The arithmetic behind the money overview, kept free of web code so it can be tested on its own.

Business and personal money never mix: every total here is for one scope.
"""
import calendar
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from datetime import date, timedelta

from app.finance_model import BILL_CATEGORIES, NOTE_PAID_GUESS, NOTE_UNCONFIRMED, REPEATS, SCOPES

CLOSED_WEEKDAYS = {6}  # Sunday
DEFAULT_DAILY_GOAL = 500
WINDOWS = (7, 14, 30)
JOB_LATE_DAYS = 30


@dataclass
class JobDue:
    """What's still owed on one sent job invoice."""
    label: str
    amount_cents: int
    sent_on: date
    ro_id: int | None = None

    def late(self, today: date) -> bool:
        return (today - self.sent_on).days > JOB_LATE_DAYS


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


def parse_past_date(text: str, today: date) -> date:
    """A day something already happened: like parse_date, but never in the future."""
    found = parse_date(text, today)
    has_year = "-" in text or text.count("/") == 2
    if found > today and not has_year:
        found = found.replace(year=found.year - 1)
    if found > today:
        raise ValueError(f"'{text}' is in the future")
    return found


def days_late(bill) -> int | None:
    """How many days after its due date a paid bill was paid. None when it isn't paid or the day isn't known."""
    if getattr(bill, "status", None) != "paid" or not getattr(bill, "paid_on", None):
        return None
    return max((bill.paid_on - bill.due_date).days, 0)


def is_unconfirmed(bill) -> bool:
    return getattr(bill, "note", None) == NOTE_UNCONFIRMED


def paid_date_guessed(bill) -> bool:
    return getattr(bill, "note", None) == NOTE_PAID_GUESS


def on_time(bills, today: date, days: int = 90) -> dict:
    """How bills due in the last `days` days were paid: late count, average days late, fees, and the late ones."""
    since = today - timedelta(days=days)
    paid, late, fees = [], [], 0
    for bill in bills:
        if bill.status == "skipped" or bill.due_date < since or bill.due_date > today:
            continue
        if bill.category == "fees":
            fees += bill.amount_cents
            continue
        late_by = days_late(bill)
        if late_by is None:
            continue
        paid.append(bill)
        if late_by > 0:
            late.append(bill)
    open_late = [b for b in bills if b.status == "open" and b.category != "fees" and since <= b.due_date < today]
    return {
        "days": days,
        "paid": len(paid),
        "late": len(late),
        "average_late": round(sum(days_late(b) for b in late) / len(late)) if late else 0,
        "fees": fees,
        "still_open": len(open_late),
        "late_bills": sorted(late, key=lambda b: b.paid_on, reverse=True)[:8],
    }


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
    jobs_due: list = field(default_factory=list)   # unpaid balances on sent job invoices
    jobs_in: int = 0                               # the part of expected_in that comes from jobs

    @property
    def main_account_id(self):
        """The account a bill most likely comes out of: the one with the most money that isn't held for anyone."""
        best = max(self.accounts, key=lambda a: a.balance_cents - a.held_cents, default=None)
        return best.id if best else None

    @property
    def owed(self) -> int:
        return self.overdue + self.due_in_window

    @property
    def short(self) -> int:
        """How much more has to come in by the end of the window. Zero when covered."""
        return max(self.owed - self.cash - self.expected_in, 0)


def totals(accounts, bills, incoming, today: date, window_days: int, jobs=()) -> dict:
    end = today + timedelta(days=window_days - 1)
    out = {scope: ScopeTotals() for scope in SCOPES}
    for account in accounts:
        if not account.active:
            continue
        t = out[account.scope]
        t.accounts.append(account)
        t.cash += account.balance_cents - account.held_cents
        t.held += account.held_cents
    entered = {(b.name.lower(), b.due_date) for b in bills if b.status == "open"}
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
        # A repeating bill comes due again inside a long window (rent in the 30-day view): count those too.
        following = next_due(bill.due_date, bill.repeats)
        while following is not None and following <= end:
            if following >= today and (bill.name.lower(), following) not in entered:
                t.due_in_window += bill.amount_cents
                t.window_bills.append(SimpleNamespace(
                    id=None, name=bill.name, scope=bill.scope, category=bill.category, amount_cents=bill.amount_cents,
                    due_date=following, repeats=bill.repeats, projected=True))
                entered.add((bill.name.lower(), following))
            following = next_due(following, bill.repeats)
    for t in out.values():
        t.window_bills.sort(key=lambda b: (b.due_date, b.name))
    for item in sorted(incoming, key=lambda i: (i.expected_date is None, i.expected_date or today, i.source)):
        if item.status != "expected":
            continue
        t = out[item.scope]
        t.incoming.append(item)
        # Late money isn't counted on: it shows as late until the owner gives it a new date.
        if item.expected_date is not None and today <= item.expected_date <= end:
            t.expected_in += item.amount_cents
    for job in sorted(jobs, key=lambda j: j.sent_on):
        t = out["business"]
        t.jobs_due.append(job)
        # A sent invoice is due on receipt. One that has sat a month is late and isn't counted on.
        if (today - job.sent_on).days <= JOB_LATE_DAYS:
            t.expected_in += job.amount_cents
            t.jobs_in += job.amount_cents
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


def cash_line(t: ScopeTotals, today: date, end: date) -> list[dict]:
    """Shop cash at the end of each day: past-due bills land today, other bills on their due date,
    money owed on the day it's expected. Job invoices have no date, so they stay off the line."""
    moves: dict[date, int] = {}
    for bill in t.overdue_bills:
        moves[today] = moves.get(today, 0) - bill.amount_cents
    for bill in t.window_bills:
        moves[bill.due_date] = moves.get(bill.due_date, 0) - bill.amount_cents
    for item in t.incoming:
        if item.expected_date is not None and today <= item.expected_date <= end:
            moves[item.expected_date] = moves.get(item.expected_date, 0) + item.amount_cents
    line, running, day = [], t.cash, today
    while day <= end:
        running += moves.get(day, 0)
        line.append({"day": day, "cents": running, "change": moves.get(day, 0)})
        day += timedelta(days=1)
    return line


def week_bounds(today: date) -> tuple[date, date, date]:
    """Monday of this week, Monday of last week, and tomorrow."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday, this_monday - timedelta(days=7), today + timedelta(days=1)


def paid_out(bills, scope: str, start: date, end_exclusive: date) -> int:
    return sum(b.amount_cents for b in bills
               if b.scope == scope and b.status == "paid" and b.paid_on and start <= b.paid_on < end_exclusive)


# ---- bank statement files -----------------------------------------------------

@dataclass
class StatementRow:
    day: date
    description: str
    amount: int            # money in is positive, money out negative
    balance: int | None = None


def _money(text: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    value = cents(text.strip("()"))
    return -abs(value) if negative else value


def parse_statement(text: str, today: date) -> tuple[list[StatementRow], list[str]]:
    """A bank's CSV download. Finds the header row (banks put summary lines above it), then reads
    the date, description, amount (or debit and credit) and running balance when there is one."""
    import csv, io
    rows = list(csv.reader(io.StringIO(text)))
    for start, header in enumerate(rows):
        names = [h.strip().lower() for h in header]
        if any("date" in n for n in names) and any(n in ("amount", "debit", "credit", "withdrawals", "deposits") or "amount" in n for n in names):
            break
    else:
        return [], ["Couldn't find the column names in that file. It needs a date and an amount column."]

    def col(*words, avoid=()):
        for i, n in enumerate(names):
            if any(w in n for w in words) and not any(a in n for a in avoid):
                return i
        return None
    date_i = col("date")
    desc_i = col("description", "payee", "memo", "details", "name")
    amount_i = col("amount", avoid=("balance",))
    debit_i, credit_i = col("debit", "withdrawal"), col("credit", "deposit")
    bal_i = col("balance", "running bal")
    out, problems = [], []
    for number, raw in enumerate(rows[start + 1:], start + 2):
        if not any(cell.strip() for cell in raw):
            continue
        get = lambda i: raw[i] if i is not None and i < len(raw) else ""
        try:
            day = parse_date(get(date_i), today)
            if amount_i is not None:
                amount = _money(get(amount_i))
            else:
                out_amt, in_amt = _money(get(debit_i)), _money(get(credit_i))
                amount = (abs(in_amt) if in_amt else 0) - (abs(out_amt) if out_amt else 0)
            if amount is None:
                continue  # a starting-balance line with no amount
            out.append(StatementRow(day, get(desc_i).strip()[:120], amount, _money(get(bal_i))))
        except ValueError as exc:
            if len(problems) < 3:
                problems.append(f"Row {number}: {exc}.")
    return out, problems


def statement_balance(rows: list[StatementRow]) -> tuple[int, date] | None:
    """The running balance after the last transaction on the latest date in the file."""
    dated = [(r.day, i, r) for i, r in enumerate(rows) if r.balance is not None]
    if not dated:
        return None
    latest = max(day for day, _, _ in dated)
    same_day = [r for day, _, r in dated if day == latest]
    newest_first = rows[0].day > rows[-1].day
    # On the latest day, the balance the day's rows lead to is the last one in date order.
    return (same_day[0] if newest_first else same_day[-1]).balance, latest


def bills_that_look_paid(rows: list[StatementRow], bills) -> list:
    """Open bills whose exact amount went out within ten days of the due date."""
    found = []
    for bill in bills:
        if bill.status != "open" or getattr(bill, "projected", False):
            continue
        for r in rows:
            if r.amount == -bill.amount_cents and abs((r.day - bill.due_date).days) <= 10:
                found.append((bill, r))
                break
    return found


# ---- pasted figures ----------------------------------------------------------

PASTE_HELP = (
    "balance | Shop checking | 250.00 | business",
    "balance | Apple Cash | 100.00 | business | held 100.00",
    "bill | Shop power | 300.00 | 12/4 | business | utilities | monthly",
    "bill | Phone | 70.00 | 12/9 | personal | phone | monthly | unconfirmed",
    "owed | Pat Sample | 500.00 | 11/30",
    "paid | Shop power",
    "paid | Shop power | 11/10 | guess",
    "got | Pat Sample",
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
    unconfirmed: bool = False  # a bill found in mail that he hasn't confirmed
    guess: bool = False  # a paid date we're not sure of


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
                line = PasteLine(kind, parts[1][:80])
                for word in parts[2:]:
                    if not word:
                        continue
                    if kind == "paid" and word.lower() == "guess":
                        line.guess = True
                    elif kind == "paid":
                        line.when = parse_past_date(word, today)
                    else:
                        raise ValueError(f"didn't understand '{word}'")
                lines.append(line)
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
                elif kind == "bill" and low == "unconfirmed":
                    line.unconfirmed = True
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
