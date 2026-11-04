"""Pure reconciliation and reporting logic: no database, no web framework."""
import re
from collections import defaultdict
from decimal import Decimal
from difflib import SequenceMatcher
from app import finance_rules as R

EXCLUDED_DUPES = ("confirmed_duplicate", "superseded", "amount_conflict")


def cents(text) -> int:
    value = Decimal(str(text).replace(",", "").replace("$", "").strip())
    return int((value * 100).to_integral_value())


def money(value: int | None) -> str:
    if value is None:
        return "—"
    return f"-${abs(value) / 100:,.2f}" if value < 0 else f"${value / 100:,.2f}"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return a == b or SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.82


def _digit_misread(a: int, b: int) -> bool:
    """OCR reads a leading 1 as 4 or 7. True when two amounts differ only that way."""
    x, y = str(abs(a)), str(abs(b))
    if len(x) == len(y):
        diffs = [(p, q) for p, q in zip(x, y) if p != q]
        return len(diffs) == 1 and set(diffs[0]) <= set("147")
    return abs(len(x) - len(y)) == 1 and (x.endswith(y) or y.endswith(x))


def dedupe_ocr(rows):
    by_day = defaultdict(list)
    for row in rows:
        by_day[(row.account_id, row.posted_date)].append(row)
    for day_rows in by_day.values():
        groups = []
        for row in day_rows:
            for group in groups:
                head = group[0]
                same_sign = (row.amount_cents > 0) == (head.amount_cents > 0)
                if same_sign and (row.amount_cents == head.amount_cents or _digit_misread(row.amount_cents, head.amount_cents)) and _similar(row.description, head.description):
                    group.append(row)
                    break
            else:
                groups.append([row])
        for group in groups:
            pages = defaultdict(list)
            for row in group:
                pages[(row.source_file, row.source_page)].append(row)
            keep_count = max(map(len, pages.values()))
            amounts = sorted({r.amount_cents for r in group}, key=abs)
            ordered = sorted(group, key=lambda r: (abs(r.amount_cents), r.id))
            keepers, extras = ordered[:keep_count], ordered[keep_count:]
            for row in keepers:
                if len(amounts) > 1:
                    row.needs_review, row.review_reason = True, "Scanned copies disagree on the amount; lower value kept"
            for row in extras:
                row.duplicate_status = "amount_conflict" if row.amount_cents != keepers[0].amount_cents else "confirmed_duplicate"
                row.duplicate_of_id = keepers[0].id


def _ref(text: str) -> str:
    match = re.search(r"(?:transaction#|confirmation#|conf#|jpm99|99)\s*([a-z0-9]{6,})\s*$", text, re.I) or re.search(r"([A-Za-z0-9]{9,})\s*$", text)
    return match.group(1).lower()[-8:] if match else ""


def match_transfers(rows):
    pool = [r for r in rows if r.report_group == R.TRANSFER and r.duplicate_status == "unique"]
    by_amount = defaultdict(list)
    for row in pool:
        row.transfer_status, row.transfer_match_id = "counterparty_missing", None
        by_amount[abs(row.amount_cents)].append(row)
    for group in by_amount.values():
        outs = [r for r in group if r.amount_cents < 0]
        ins = [r for r in group if r.amount_cents > 0]
        for out in outs:
            options = [i for i in ins if i.transfer_status != "matched" and i.account_id != out.account_id and abs((i.posted_date - out.posted_date).days) <= 4]
            options.sort(key=lambda i: (_ref(i.description) != _ref(out.description), abs((i.posted_date - out.posted_date).days)))
            if options:
                match = options[0]
                out.transfer_status = match.transfer_status = "matched"
                out.transfer_match_id, match.transfer_match_id = match.id, out.id


def pnl(rows) -> dict:
    t = defaultdict(int)
    categories = defaultdict(int)
    for r in rows:
        t[r.report_group] += r.amount_cents
        categories[(r.report_group, r.category)] += r.amount_cents
    revenue, cogs, opex = t[R.REVENUE], -t[R.COGS], -t[R.OPEX]
    gross = revenue - cogs
    net = gross - opex
    return {"revenue": revenue, "cogs": cogs, "gross": gross, "opex": opex, "net": net,
            "gross_margin": round(gross * 100 / revenue, 1) if revenue else None,
            "net_margin": round(net * 100 / revenue, 1) if revenue else None,
            "capex": -t[R.CAPEX], "debt": -t[R.DEBT], "interest": -t[R.INTEREST],
            "owner_contrib": t[R.CONTRIB], "owner_draw": -t[R.DRAW], "personal": -t[R.PERSONAL],
            "unconfirmed": t[R.UNCONFIRMED], "unclassified": t[R.UNCLASSIFIED], "transfers": t[R.TRANSFER],
            "cash_in": sum(r.amount_cents for r in rows if r.amount_cents > 0),
            "cash_out": -sum(r.amount_cents for r in rows if r.amount_cents < 0),
            "categories": dict(categories)}


def amortize(principal: int, months: int, premium_pct: float) -> dict:
    total = principal * (1 + premium_pct / 100)
    payment = total / months if months else 0
    return {"principal": principal, "months": months, "total": round(total), "payment": round(payment), "cost": round(total - principal),
            "apr": round(_apr(principal, payment, months), 1) if months else None}


def _apr(principal, payment, months) -> float:
    low, high = 0.0, 1.0
    if payment * months <= principal:
        return 0.0
    for _ in range(80):
        mid = (low + high) / 2
        value = payment * (1 - (1 + mid) ** -months) / mid if mid else payment * months
        low, high = (mid, high) if value > principal else (low, mid)
    return low * 1200
