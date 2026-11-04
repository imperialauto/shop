"""Finances: owner-only. Every route goes through require_owner."""
import csv, io
from collections import defaultdict
from datetime import date, datetime
import os
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlalchemy.orm import Session
from app.auth import get_current_user, is_finance_owner
from app.database import get_db
from app import finance_engine as E, finance_rules as R
from app.finance_model import FinAccount, FinAudit, FinImportBatch, FinReview, FinRule, FinTransaction

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
templates.env.globals.update(money=E.money)
MAX_UPLOAD = 10 * 1024 * 1024
PAGE = 100
TABS = [("overview", "Overview"), ("ledger", "Ledger"), ("recon", "Reconciliation"), ("reports", "Reports"), ("accounts", "Accounts"), ("investor", "Investor Center"), ("import", "Import")]
USES = [("Tire machine and balancer", 900000), ("Parts inventory", 600000), ("Shop lift", 700000), ("Mobile repair vehicle and tools", 500000), ("Detailing and wrap equipment", 400000), ("Marketing", 400000)]


def audit(db, request, actor, action, detail="", allowed=True):
    db.add(FinAudit(actor=actor, action=action, detail=detail[:300], ip=request.client.host if request.client else None, allowed=allowed))
    db.commit()


def require_owner(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        raise HTTPException(status_code=401, detail="Sign in required")
    if not is_finance_owner(user.username):
        audit(db, request, user.username, "access_denied", request.url.path, allowed=False)
        raise HTTPException(status_code=403, detail="Finances are restricted to the business owner")
    return user


def scoped(q, db, f):
    accounts = {a.last4: a for a in db.query(FinAccount)}
    if f["account"]:
        q = q.filter(FinTransaction.account_id == accounts[f["account"]].id) if f["account"] in accounts else q.filter(False)
    elif f["scope"] != "all":
        ids = [a.id for a in accounts.values() if (a.designation == "personal") == (f["scope"] == "personal")]
        q = q.filter(FinTransaction.account_id.in_(ids))
    if f["year"]:
        q = q.filter(FinTransaction.posted_date >= date(f["year"], 1, 1), FinTransaction.posted_date <= date(f["year"], 12, 31))
    if f["month"] and f["year"]:
        end = date(f["year"] + (f["month"] == 12), f["month"] % 12 + 1, 1)
        q = q.filter(FinTransaction.posted_date >= date(f["year"], f["month"], 1), FinTransaction.posted_date < end)
    if f["division"]:
        q = q.filter(FinTransaction.division == f["division"])
    if f["group"]:
        q = q.filter(FinTransaction.report_group == f["group"])
    return q


def filters(request):
    p = request.query_params
    num = lambda k: int(p[k]) if p.get(k, "").isdigit() else None
    basis = p.get("basis") if p.get("basis") in ("verified", "all") else "verified"
    scope = p.get("scope") if p.get("scope") in ("business", "personal", "all") else "business"
    return {"year": num("year"), "month": num("month"), "account": p.get("account", ""), "division": p.get("division", ""),
            "group": p.get("group", ""), "q": p.get("q", "")[:80], "basis": basis, "scope": scope, "status": p.get("status", "")}


def monthly(db, f):
    rows = scoped(E.reportable(db, f["basis"]), db, {**f, "month": None}).all()
    by = defaultdict(list)
    for r in rows:
        by[r.posted_date.strftime("%Y-%m")].append(r)
    return {m: E.pnl(v) for m, v in sorted(by.items())}, rows


def investor_context(db, request):
    p = request.query_params
    closed = E.closed_months(db)
    rows = [r for r in E.reportable(db).all() if r.posted_date.strftime("%Y-%m") in closed and r.designation != "personal"]
    ids = {a.id for a in db.query(FinAccount).filter_by(designation="business")}
    actual = E.pnl([r for r in rows if r.account_id in ids]) if rows else None
    n = len(closed) or 1
    base_rev = (actual["revenue"] // n) if actual else 0
    base_margin = (actual["gross"] / actual["revenue"]) if actual and actual["revenue"] else 0
    base_opex = (actual["opex"] // n) if actual else 0
    def val(key, default):
        try:
            return float(p.get(key, default))
        except ValueError:
            return default
    premium = max(0.0, min(val("premium", 20), 100))
    overhead = int(val("overhead", base_opex / 100) * 100)
    margin = max(1.0, min(val("margin", round(base_margin * 100, 1) or 50), 95)) / 100
    def row(amount, months):
        a = E.amortize(amount, months, premium)
        needed = int((overhead + a["payment"]) / margin)
        share = round(a["payment"] * 100 / base_rev, 1) if base_rev else None
        return {**a, "needed": needed, "gap": needed - base_rev, "share": share}
    small = [row(a, m) for a in (500000, 1000000) for m in (6, 12)]
    major = [row(a, m) for a in (3000000, 4000000, 5000000) for m in (12, 18, 24)]
    return {"closed": closed, "actual": actual, "base_rev": base_rev, "premium": premium, "overhead": overhead, "margin": round(margin * 100, 1), "small": small, "major": major, "uses": USES, "uses_total": sum(u[1] for u in USES)}


@router.get("/", response_class=HTMLResponse)
def page(request: Request, tab: str = "overview", page: int = 1, db: Session = Depends(get_db)):
    try:
        user = require_owner(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=302)
        raise
    tab = tab if tab in dict(TABS) else "overview"
    f = filters(request)
    audit(db, request, user.username, "view", tab)
    ctx = {"request": request, "user": user, "tab": tab, "tabs": TABS, "f": f, "groups": R.GROUPS, "categories": R.CATEGORIES,
           "accounts": db.query(FinAccount).order_by(FinAccount.last4).all(), "years": [2023, 2024, 2025, 2026],
           "open_exceptions": E.reportable(db).filter(FinTransaction.needs_review.is_(True)).count(), "closed_months": E.closed_months(db)}
    if tab in ("overview", "reports"):
        months, rows = monthly(db, f)
        ctx.update(months=months, total=E.pnl(rows), coverage=E.coverage(db))
        if f["year"]:
            ctx["months"] = {m: v for m, v in months.items() if m.startswith(str(f["year"]))}
            ctx["total"] = E.pnl([r for r in rows if r.posted_date.year == f["year"]])
        expense = {k[1]: -v for k, v in ctx["total"]["categories"].items() if k[0] in (R.COGS, R.OPEX) and v < 0}
        ctx["expense"] = sorted(expense.items(), key=lambda x: -x[1])
    if tab == "ledger":
        q = scoped(db.query(FinTransaction), db, f)
        if f["basis"] == "verified":
            q = q.filter(FinTransaction.verification_status == "statement_tied")
        if f["status"] == "excluded":
            q = q.filter(FinTransaction.duplicate_status.in_(E.EXCLUDED_DUPES))
        elif f["status"] != "all":
            q = q.filter(FinTransaction.duplicate_status.notin_(E.EXCLUDED_DUPES))
        if f["q"]:
            like = f"%{f['q']}%"
            q = q.filter(or_(FinTransaction.description.ilike(like), FinTransaction.category.ilike(like)))
        ctx["count"], ctx["page"] = q.count(), max(page, 1)
        ctx["rows"] = q.order_by(FinTransaction.posted_date.desc(), FinTransaction.id.desc()).offset((ctx["page"] - 1) * PAGE).limit(PAGE).all()
        ctx["names"] = {a.id: a for a in ctx["accounts"]}
    if tab == "recon":
        ctx["statements"], ctx["coverage"] = E.statement_status(db), E.coverage(db)
        queue = E.reportable(db).filter(FinTransaction.needs_review.is_(True)).all()
        grouped = defaultdict(list)
        for r in queue:
            grouped[r.review_reason or "Needs review"].append(r)
        ctx["queue"] = sorted(grouped.items(), key=lambda kv: -sum(abs(r.amount_cents) for r in kv[1]))
        base = db.query(FinTransaction)
        ctx["recon"] = {"tied": base.filter_by(verification_status="statement_tied").count(), "ocr_kept": base.filter_by(source_type="ocr", duplicate_status="unique").count(),
                        "superseded": base.filter_by(duplicate_status="superseded").count(), "dupes": base.filter_by(duplicate_status="confirmed_duplicate").count(),
                        "conflicts": base.filter_by(duplicate_status="amount_conflict").count(), "matched": base.filter_by(transfer_status="matched").count() // 2,
                        "missing": base.filter_by(transfer_status="counterparty_missing").count()}
        ctx["names"] = {a.id: a for a in ctx["accounts"]}
    if tab == "accounts":
        stats = {}
        for a in ctx["accounts"]:
            rows = db.query(FinTransaction).filter(FinTransaction.account_id == a.id, FinTransaction.duplicate_status.notin_(E.EXCLUDED_DUPES)).all()
            stats[a.id] = {"count": len(rows), "first": min((r.posted_date for r in rows), default=None), "last": max((r.posted_date for r in rows), default=None),
                           "tied": sum(r.verification_status == "statement_tied" for r in rows)}
        ctx["stats"] = stats
    if tab == "investor":
        ctx["inv"] = investor_context(db, request)
    if tab == "import":
        ctx["batches"] = db.query(FinImportBatch).order_by(FinImportBatch.id.desc()).limit(25).all()
        ctx["activity"] = db.query(FinAudit).order_by(FinAudit.id.desc()).limit(25).all()
    return templates.TemplateResponse("finance/index.html", ctx)


@router.post("/import")
async def do_import(request: Request, file: UploadFile = File(...), account: str = Form(""), db: Session = Depends(get_db), user=Depends(require_owner)):
    raw = await file.read(MAX_UPLOAD + 1)
    name = (file.filename or "upload.csv")[:200]
    if len(raw) > MAX_UPLOAD or not name.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Upload a CSV under 10 MB")
    try:
        batch = E.import_csv(db, raw, name, user.username, account)
    except (ValueError, UnicodeDecodeError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    audit(db, request, user.username, "import", f"{name}: {batch.rows_added} added, {batch.rows_skipped} already present")
    return RedirectResponse("/finance/?tab=import", status_code=303)


@router.post("/txn/{txn_id}/review")
def review(request: Request, txn_id: int, category: str = Form(...), report_group: str = Form(...), designation: str = Form("business"), division: str = Form("unassigned"),
           note: str = Form(""), apply_pattern: str = Form(""), db: Session = Depends(get_db), user=Depends(require_owner)):
    row = db.get(FinTransaction, txn_id)
    if not row:
        raise HTTPException(status_code=404)
    if report_group not in R.GROUPS or category not in R.CATEGORIES:
        raise HTTPException(status_code=400, detail="Unknown category")
    before = f"{row.report_group}/{row.category}/{row.designation}"
    row.report_group, row.category, row.designation, row.division = report_group, category, designation, division
    row.needs_review, row.review_reason, row.confidence, row.locked = False, None, "owner", True
    db.add(FinReview(transaction_id=row.id, actor=user.username, action="classified", before_value=before, after_value=f"{report_group}/{category}/{designation}", note=note[:500] or None))
    pattern = apply_pattern.strip()
    if len(pattern) >= 4 and not db.query(FinRule).filter_by(pattern=pattern).first():
        db.add(FinRule(pattern=pattern, report_group=report_group, category=category, designation=designation, division=division, note=None))
        db.flush()
        E.rebuild(db)
    db.commit()
    audit(db, request, user.username, "review", f"transaction {row.id}")
    return RedirectResponse("/finance/?tab=recon", status_code=303)


@router.post("/txn/{txn_id}/duplicate")
def duplicate(request: Request, txn_id: int, action: str = Form(...), db: Session = Depends(get_db), user=Depends(require_owner)):
    row = db.get(FinTransaction, txn_id)
    if not row or action not in ("exclude", "restore"):
        raise HTTPException(status_code=400)
    before = row.duplicate_status
    row.duplicate_status, row.locked = ("confirmed_duplicate" if action == "exclude" else "owner_kept"), True
    db.add(FinReview(transaction_id=row.id, actor=user.username, action=f"duplicate_{action}", before_value=before, after_value=row.duplicate_status))
    db.commit()
    audit(db, request, user.username, "duplicate", f"transaction {row.id} {action}")
    return RedirectResponse("/finance/?tab=ledger&basis=all&status=all", status_code=303)


def _csv(name, header, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(header)
    w.writerows(rows)
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def safe(value):
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "@") else text  # blocks spreadsheet formula injection


@router.get("/export/ledger.csv")
def export_ledger(request: Request, db: Session = Depends(get_db), user=Depends(require_owner)):
    f = filters(request)
    rows = scoped(db.query(FinTransaction), db, f).order_by(FinTransaction.posted_date).all()
    names = {a.id: a.name for a in db.query(FinAccount)}
    audit(db, request, user.username, "export", f"ledger {len(rows)} rows")
    return _csv("imperial_ledger.csv", ["date", "account", "amount", "description", "group", "category", "designation", "division", "duplicate_status", "transfer_status", "verification", "source_file", "source_page"],
                [[r.posted_date, names[r.account_id], r.amount_cents / 100, safe(r.description), r.report_group, r.category, r.designation, r.division, r.duplicate_status, r.transfer_status, r.verification_status, r.source_file, r.source_page] for r in rows])


@router.get("/export/pnl.csv")
def export_pnl(request: Request, db: Session = Depends(get_db), user=Depends(require_owner)):
    f = filters(request)
    months, _ = monthly(db, f)
    audit(db, request, user.username, "export", "pnl")
    return _csv("imperial_pnl.csv", ["month", "basis", "revenue", "cogs", "gross_profit", "operating_expenses", "net_before_unclassified", "unclassified", "unconfirmed_receipts"],
                [[m, f["basis"], p["revenue"] / 100, p["cogs"] / 100, p["gross"] / 100, p["opex"] / 100, p["net"] / 100, p["unclassified"] / 100, p["unconfirmed"] / 100] for m, p in months.items()])


@router.get("/export/investor.pdf")
def export_investor(request: Request, db: Session = Depends(get_db), user=Depends(require_owner)):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    inv = investor_context(db, request)
    audit(db, request, user.username, "export", "investor pdf")
    buf, st = io.BytesIO(), getSampleStyleSheet()
    story = [Paragraph("Imperial Auto Care LLC: Funding Analysis", st["Title"]),
             Paragraph(f"Prepared {datetime.utcnow():%B %d, %Y}. Not audited. Cash basis, from bank statements.", st["Normal"]), Spacer(1, 10),
             Paragraph("Historical actual", st["Heading2"])]
    if inv["actual"]:
        a = inv["actual"]
        story.append(Paragraph(f"Statement-tied month(s): {', '.join(inv['closed'])}. Revenue {E.money(a['revenue'])}, gross profit {E.money(a['gross'])}, operating expenses {E.money(a['opex'])}. "
                               f"Not yet classified {E.money(a['unclassified'])}; unconfirmed receipts {E.money(a['unconfirmed'])}, excluded from revenue.", st["Normal"]))
    else:
        story.append(Paragraph("No month is fully statement-tied yet.", st["Normal"]))
    story += [Spacer(1, 10), Paragraph("Assumptions (not results)", st["Heading2"]),
              Paragraph(f"Flat premium {inv['premium']}%. Monthly overhead {E.money(inv['overhead'])}. Gross margin {inv['margin']}%.", st["Normal"])]
    for title, rows in (("Projection: small investments", inv["small"]), ("Projection: major funding", inv["major"])):
        data = [["Amount", "Months", "Payment", "Total repaid", "APR est.", "Revenue needed/mo"]] + [[E.money(r["principal"]), r["months"], E.money(r["payment"]), E.money(r["total"]), f"{r['apr']}%", E.money(r["needed"])] for r in rows]
        t = Table(data)
        t.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8), ("LINEBELOW", (0, 0), (-1, 0), 0.5, "black")]))
        story += [Spacer(1, 10), Paragraph(title, st["Heading2"]), t]
    SimpleDocTemplate(buf, pagesize=letter).build(story)
    return Response(buf.getvalue(), media_type="application/pdf", headers={"Content-Disposition": 'attachment; filename="imperial_funding_analysis.pdf"'})
