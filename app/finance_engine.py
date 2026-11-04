"""Import, reconciliation and reporting. Pure database logic, no HTTP."""
import csv, hashlib, io, re
from collections import defaultdict
from datetime import date, timedelta
from decimal import InvalidOperation
from sqlalchemy.orm import Session
from app.finance_model import FinAccount, FinImportBatch, FinRule, FinStatement, FinTransaction
from app import finance_rules as R
from app.finance_core import EXCLUDED_DUPES, amortize, cents, dedupe_ocr, match_transfers, money, pnl  # noqa: F401



def _account(db: Session, last4: str, default_name=None, institution=None) -> FinAccount:
    last4 = re.sub(r"\D", "", last4)[-4:]
    if len(last4) != 4:
        raise ValueError("Account must include its last four digits")
    row = db.query(FinAccount).filter_by(last4=last4).first()
    if row:
        return row
    row = FinAccount(institution=institution or "Unknown", last4=last4, name=default_name or f"Account {last4}", designation="business", status="unknown")
    db.add(row)
    db.flush()
    return row


def _key(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


def detect(header: list[str]) -> str:
    h = {c.strip().lower() for c in header}
    if {"canonical_id", "signed_cashflow", "source_pdf"} <= h:
        return "ocr"
    if {"account_last4", "institution", "name", "designation"} <= h:
        return "accounts"
    if {"account_last4", "period_start", "beginning", "ending"} <= h:
        return "statement_controls"
    if {"account_last4", "date", "signed_amount", "description", "source_file"} <= h:
        return "statement_lines"
    if {"pattern", "report_group", "category"} <= h:
        return "rules"
    if {"date", "description", "amount"} <= h:
        return "bank_csv"
    raise ValueError("Unrecognised file. Expected a statement, OCR, rules or bank CSV with a header row.")


def import_csv(db: Session, raw: bytes, filename: str, actor: str, account_last4: str = "") -> FinImportBatch:
    """Idempotent: every row has a stable key, and a repeat of an identical row in
    one file gets an occurrence number so real same-day repeats survive."""
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    kind = detect(reader.fieldnames or [])
    if kind == "bank_csv" and not account_last4:
        raise ValueError("Choose the account this bank export belongs to.")
    batch = FinImportBatch(filename=filename[:200], kind=kind, sha256=hashlib.sha256(raw).hexdigest(), imported_by=actor)
    db.add(batch)
    db.flush()
    existing = {k for (k,) in db.query(FinTransaction.import_key)}
    seen = defaultdict(int)
    for row in reader:
        batch.rows_read += 1
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if kind == "rules":
            added = _import_rule(db, row)
        elif kind == "accounts":
            added = _import_account(db, row)
        elif kind == "statement_controls":
            added = _import_control(db, row, batch)
        else:
            added = _import_txn(db, row, kind, batch, seen, existing, account_last4)
        batch.rows_added += 1 if added else 0
        batch.rows_skipped += 0 if added else 1
    db.flush()
    rebuild(db)
    db.commit()
    return batch


def _import_account(db, row) -> bool:
    if row["designation"] not in ("business", "personal", "mixed"):
        raise ValueError(f"Unknown designation {row['designation']}")
    account = _account(db, row["account_last4"], row["name"], row["institution"])
    changed = (account.institution, account.name, account.designation, account.status) != (row["institution"], row["name"], row["designation"], row.get("status") or "unknown")
    account.institution, account.name, account.designation, account.status = row["institution"], row["name"], row["designation"], row.get("status") or "unknown"
    return changed


def _import_rule(db, row) -> bool:
    if row["report_group"] not in R.GROUPS:
        raise ValueError(f"Unknown report group {row['report_group']}")
    if db.query(FinRule).filter_by(pattern=row["pattern"]).first():
        return False
    db.add(FinRule(pattern=row["pattern"], report_group=row["report_group"], category=row["category"],
                   designation=row.get("designation") or "business", division=row.get("division") or "unassigned",
                   needs_review=row.get("needs_review", "").lower() in ("1", "true", "yes"), note=row.get("note") or None))
    db.flush()
    return True


def _import_control(db, row, batch) -> bool:
    account = _account(db, row["account_last4"])
    start, end = date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"])
    if db.query(FinStatement).filter_by(account_id=account.id, period_start=start, period_end=end).first():
        return False
    db.add(FinStatement(account_id=account.id, period_start=start, period_end=end, beginning_cents=cents(row["beginning"]),
                        deposits_cents=cents(row["deposits"]), withdrawals_cents=abs(cents(row["withdrawals"])),
                        ending_cents=cents(row["ending"]), source_file=row.get("source_file"), batch_id=batch.id))
    db.flush()
    return True


def _import_txn(db, row, kind, batch, seen, existing, account_last4) -> bool:
    try:
        if kind == "ocr":
            last4 = re.sub(r"\D", "", row["account"])[-4:]
            posted, amount, description = row["date"], cents(row["signed_cashflow"]), row["description"]
            base = (row["canonical_id"], row["source_pdf"], row["source_page"])
            source = "ocr"
        elif kind == "statement_lines":
            last4 = row["account_last4"]
            posted, amount, description = row["date"], cents(row["signed_amount"]), row["description"]
            base = (last4, posted, amount, description, row["source_file"])
            source = "statement"
        else:
            last4 = account_last4
            posted, amount, description = row["date"], cents(row["amount"]), row["description"]
            base = (last4, posted, amount, description)
            source = "bank_csv"
        posted = date.fromisoformat(posted)
    except (KeyError, ValueError, InvalidOperation) as exc:
        raise ValueError(f"Row {batch.rows_read + 1} could not be read") from exc
    seen[base] += 1
    key = _key(source, *base, seen[base])
    if key in existing:
        return False
    existing.add(key)
    account = _account(db, last4)
    db.add(FinTransaction(
        import_key=key, batch_id=batch.id, account_id=account.id, source_type=source, source_txn_id=row.get("canonical_id") or None,
        posted_date=posted, amount_cents=amount, description=description, source_file=row.get("source_pdf") or row.get("source_file"),
        source_page=int(row["source_page"]) if row.get("source_page", "").isdigit() else None,
        statement_period=row.get("statement_period"), source_category=row.get("proposed_category"), source_issue=(row.get("issues") or None)))
    return True


# ── Reconciliation ────────────────────────────────────────────────────────

def statement_for(statements, row):
    for s in statements:
        if s.account_id == row.account_id and s.period_start <= row.posted_date <= s.period_end:
            return s
    return None


def statement_status(db: Session) -> list[dict]:
    result = []
    for s in db.query(FinStatement).order_by(FinStatement.period_start, FinStatement.account_id):
        rows = db.query(FinTransaction).filter(FinTransaction.account_id == s.account_id, FinTransaction.source_type == "statement",
                                               FinTransaction.posted_date >= s.period_start, FinTransaction.posted_date <= s.period_end).all()
        deposits = sum(r.amount_cents for r in rows if r.amount_cents > 0)
        withdrawals = -sum(r.amount_cents for r in rows if r.amount_cents < 0)
        arithmetic = s.beginning_cents + s.deposits_cents - s.withdrawals_cents - s.ending_cents
        ties = bool(rows) and arithmetic == 0 and deposits == s.deposits_cents and withdrawals == s.withdrawals_cents
        result.append({"statement": s, "rows": len(rows), "deposits": deposits, "withdrawals": withdrawals, "ties": ties,
                       "variance": (s.beginning_cents + deposits - withdrawals) - s.ending_cents,
                       "status": "Tied" if ties else ("Controls only" if not rows else "Out of balance")})
    return result


def rebuild(db: Session):
    db.flush()
    rows = db.query(FinTransaction).order_by(FinTransaction.id).all()
    rules = sorted(db.query(FinRule).all(), key=lambda r: -len(r.pattern))
    tied = [s["statement"] for s in statement_status(db) if s["ties"]]
    accounts = {a.id: a for a in db.query(FinAccount)}
    open_rows = [r for r in rows if not r.locked]
    for row in open_rows:
        result = R.classify(row.description, row.amount_cents, rules)
        row.report_group, row.category, row.designation, row.division = result.group, result.category, result.designation, result.division
        row.confidence, row.needs_review, row.review_reason = result.confidence, result.needs_review, result.reason or None
        if accounts[row.account_id].designation == "personal" and row.report_group not in (R.TRANSFER, R.REVENUE, R.UNCONFIRMED):
            row.report_group, row.category, row.designation = R.PERSONAL, "Personal spending", "personal"
            row.needs_review, row.review_reason = False, None
        if row.duplicate_status != "owner_kept":
            row.duplicate_status, row.duplicate_of_id = "unique", None
    for row in rows:
        covered = statement_for(tied, row)
        if row.source_type == "statement":
            row.verification_status = "statement_tied" if covered else "statement_untied"
        else:
            row.verification_status = "unverified"
            if covered and not row.locked:
                row.duplicate_status, row.needs_review, row.review_reason = "superseded", False, None
    dedupe_ocr([r for r in rows if r.source_type == "ocr" and r.duplicate_status == "unique" and not r.locked])
    match_transfers(rows)
    db.flush()


# ── Reports ───────────────────────────────────────────────────────────────

def reportable(db: Session, basis: str = "verified"):
    # Records before the review window stay on file but out of reports.
    q = db.query(FinTransaction).filter(FinTransaction.duplicate_status.notin_(EXCLUDED_DUPES), FinTransaction.posted_date >= date(2023, 1, 1))
    if basis == "verified":
        q = q.filter(FinTransaction.verification_status == "statement_tied")
    return q


def coverage(db: Session, years=(2023, 2024, 2025, 2026)) -> dict:
    """month → per-account status. Absence is shown as missing, never zero."""
    accounts = db.query(FinAccount).filter_by(in_scope=True).order_by(FinAccount.last4).all()
    counts = defaultdict(lambda: defaultdict(int))
    for r in db.query(FinTransaction).filter(FinTransaction.duplicate_status.notin_(EXCLUDED_DUPES)):
        key = (r.account_id, r.posted_date.strftime("%Y-%m"))
        counts[key][r.verification_status] += 1
    months = [f"{y}-{m:02d}" for y in years for m in range(1, 13) if f"{y}-{m:02d}" <= date.today().strftime("%Y-%m")]
    grid = {}
    for m in months:
        grid[m] = {}
        for a in accounts:
            c = counts.get((a.id, m), {})
            grid[m][a.last4] = "tied" if c.get("statement_tied") else ("ocr" if sum(c.values()) else "missing")
    return {"accounts": accounts, "months": months, "grid": grid}


def closed_months(db: Session) -> list[str]:
    """Months where every in-scope business account has a tied statement."""
    biz = [a.id for a in db.query(FinAccount).filter_by(designation="business", in_scope=True)]
    active = {}
    for s in statement_status(db):
        if s["ties"]:
            active.setdefault(s["statement"].period_end.strftime("%Y-%m"), set()).add(s["statement"].account_id)
    return sorted(m for m, ids in active.items() if biz and set(biz) <= ids)


