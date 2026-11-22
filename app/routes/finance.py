"""Owner-only money overview: what's on hand, what's owed and when, what's coming in, and what the
shop needs to make each day to cover it.

Balances, bills and expected money are typed or pasted in by the owner (app/finance_model.py).
Money in comes from payments recorded on jobs plus expected money marked as received here.
Nothing on this page imports bank data, changes invoices or payments, or sends anything.
"""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app import business_settings as settings_store
from app import finance_plan as plan
from app.appointments import PHOENIX
from app.auth import get_current_user
from app.database import get_db
from app.finance_model import BILL_CATEGORIES, REPEATS, SCOPES, FinanceAccount, FinanceBill, FinanceChange, FinanceIncoming
from app.models import Invoice, Payment, RepairOrder
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
NOT_SET_UP = "The money tracker isn't set up on this server yet, so nothing was saved."


def month_boundaries(period: str | None):
    """Return an inclusive start and exclusive end for a YYYY-MM month."""
    now = datetime.now(PHOENIX)
    if not period or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        year, month = now.year, now.month
    else:
        year, month = map(int, period.split("-"))
    local_start = datetime(year, month, 1, tzinfo=PHOENIX)
    local_end = datetime(year + 1, 1, 1, tzinfo=PHOENIX) if month == 12 else datetime(year, month + 1, 1, tzinfo=PHOENIX)
    # Existing Payment.paid_at values are naive UTC timestamps.
    start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def _utc_naive(day: date) -> datetime:
    """Midnight Phoenix time on that day, as the naive UTC value Payment.paid_at uses."""
    return datetime(day.year, day.month, day.day, tzinfo=PHOENIX).astimezone(timezone.utc).replace(tzinfo=None)


def phoenix_today() -> date:
    return datetime.now(PHOENIX).date()


def daily_goal() -> int:
    try:
        value = int(float(settings_store.raw("finance_daily_goal") or ""))
    except ValueError:
        return plan.DEFAULT_DAILY_GOAL
    return value if 0 < value <= 100000 else plan.DEFAULT_DAILY_GOAL


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Finance is owner-only.")
    return user


def _shop_payments(db: Session, start: date, end_exclusive: date) -> float:
    total = (db.query(func.coalesce(func.sum(Payment.amount), 0))
             .filter(Payment.paid_at >= _utc_naive(start), Payment.paid_at < _utc_naive(end_exclusive)).scalar())
    return float(total or 0)


def _received(incoming, start: date, end_exclusive: date) -> int:
    return sum(i.amount_cents for i in incoming
               if i.status == "received" and i.scope == "business" and i.received_on and start <= i.received_on < end_exclusive)


def _recent_payments(db: Session, start: date):
    payments = (db.query(Payment)
                .options(joinedload(Payment.invoice).joinedload(Invoice.repair_order).joinedload(RepairOrder.customer))
                .filter(Payment.paid_at >= _utc_naive(start))
                .order_by(Payment.paid_at.desc()).limit(10).all())
    rows = []
    for payment in payments:
        ro = payment.invoice.repair_order if payment.invoice else None
        customer = ro.customer if ro else None
        paid = payment.paid_at.replace(tzinfo=timezone.utc) if payment.paid_at and payment.paid_at.tzinfo is None else payment.paid_at
        rows.append({
            "date": paid.astimezone(PHOENIX) if paid else None,
            "amount": float(payment.amount or 0),
            "method": payment.method or "Unspecified",
            "customer": f"{customer.first_name or ''} {customer.last_name or ''}".strip() if customer else "Customer not linked",
            "invoice_id": payment.invoice_id,
        })
    return rows


@router.get("/", response_class=HTMLResponse)
def finance_home(request: Request, window: int = 14, db: Session = Depends(get_db)):
    try:
        user = _owner(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=302)
        raise
    window = window if window in plan.WINDOWS else 14
    today = phoenix_today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    tomorrow = today + timedelta(days=1)

    ready = True
    try:
        accounts = db.query(FinanceAccount).order_by(FinanceAccount.scope, FinanceAccount.name).all()
        bills = db.query(FinanceBill).all()
        incoming = db.query(FinanceIncoming).all()
    except SQLAlchemyError:
        db.rollback()
        ready, accounts, bills, incoming = False, [], [], []

    sums = plan.totals(accounts, bills, incoming, today, window)
    business, personal = sums["business"], sums["personal"]
    goal = daily_goal()
    money_in = {
        "today": _shop_payments(db, today, tomorrow) + plan.dollars(_received(incoming, today, tomorrow)),
        "week": _shop_payments(db, week_start, tomorrow) + plan.dollars(_received(incoming, week_start, tomorrow)),
        "month": _shop_payments(db, month_start, tomorrow) + plan.dollars(_received(incoming, month_start, tomorrow)),
    }
    days_so_far = plan.working_days(week_start, today)
    unpaid_total, unpaid_count = (db.query(func.coalesce(func.sum(Invoice.total), 0), func.count(Invoice.id))
                                  .filter(Invoice.status == "sent").one())
    last_updated = max((a.as_of for a in accounts), default=None)
    paid_recently = sorted((b for b in bills if b.status == "paid" and b.paid_on and b.paid_on >= today - timedelta(days=14)),
                           key=lambda b: b.paid_on, reverse=True)

    return templates.TemplateResponse("finance/home.html", {
        "request": request,
        "user": user,
        "ready": ready,
        "today": today,
        "window": window,
        "windows": plan.WINDOWS,
        "end": sums["end"],
        "days_open": sums["days_open"],
        "business": business,
        "personal": personal,
        "need_per_day": plan.dollars(plan.per_day(business.short, sums["days_open"])),
        "goal": goal,
        "week_goal": goal * days_so_far,
        "money_in": money_in,
        "spending": plan.spending_by_category(bills, "business", month_start),
        "personal_spending": plan.spending_by_category(bills, "personal", month_start),
        "month_label": today.strftime("%B"),
        "paid_recently": paid_recently[:6],
        "activity": _recent_payments(db, today - timedelta(days=30)),
        "sent_invoice_amount": float(unpaid_total or 0),
        "sent_invoice_count": int(unpaid_count or 0),
        "last_updated": last_updated,
        "categories": BILL_CATEGORIES,
        "repeats": REPEATS,
        "scopes": SCOPES,
        "paste_help": plan.PASTE_HELP,
        "notice": request.query_params.get("notice", "")[:300],
        "problems": [p[:200] for p in request.query_params.getlist("problem")[:10]],
        "money": plan.dollars,
    })


# ---- changes ------------------------------------------------------------------

def _log(db: Session, actor: str, action: str, detail: str):
    db.add(FinanceChange(actor=actor, action=action, detail=detail[:2000]))


def _back(window, notice: str = "", problems: list[str] | None = None) -> RedirectResponse:
    query = [("window", window if window in ("7", "14", "30") else "14")]
    if notice:
        query.append(("notice", notice))
    query += [("problem", p) for p in (problems or [])[:10]]
    return RedirectResponse(f"/finance/?{urlencode(query)}", status_code=303)


def _ci(column, value: str):
    return func.lower(column) == value.strip().lower()


def _roll_forward(db: Session, bill: FinanceBill):
    following = plan.next_due(bill.due_date, bill.repeats)
    if following is None:
        return
    exists = db.query(FinanceBill).filter(_ci(FinanceBill.name, bill.name), FinanceBill.due_date == following,
                                          FinanceBill.status == "open").first()
    if exists is None:
        db.add(FinanceBill(name=bill.name, scope=bill.scope, category=bill.category, amount_cents=bill.amount_cents,
                           due_date=following, repeats=bill.repeats, note=bill.note))


def _move(db: Session, account_id, scope: str, delta_cents: int, actor: str):
    """Moves a paid bill or a received payment through one account of the same scope, so cash stays current."""
    try:
        account = db.get(FinanceAccount, int(account_id))
    except (TypeError, ValueError):
        return None
    if account is None or account.scope != scope or not account.active:
        return None
    account.balance_cents += delta_cents
    account.updated_at = datetime.now(timezone.utc)
    _log(db, actor, "balance_moved", f"{account.name} {plan.dollars(delta_cents)}")
    return account


def _mark_paid(db: Session, bill: FinanceBill, today: date, actor: str):
    bill.status, bill.paid_on = "paid", today
    _roll_forward(db, bill)
    _log(db, actor, "bill_paid", f"{bill.name} {plan.dollars(bill.amount_cents)} due {bill.due_date}")


def apply_lines(db: Session, lines, today: date, actor: str) -> tuple[int, list[str]]:
    done, problems = 0, []
    for line in lines:
        if line.kind == "balance":
            account = db.query(FinanceAccount).filter(_ci(FinanceAccount.name, line.name)).first()
            if account is None:
                account = FinanceAccount(name=line.name)
                db.add(account)
            account.balance_cents, account.held_cents, account.scope = line.amount, line.held, line.scope
            account.as_of, account.active, account.updated_at = today, True, datetime.now(timezone.utc)
            _log(db, actor, "balance", f"{line.name} {plan.dollars(line.amount)} held {plan.dollars(line.held)} {line.scope}")
        elif line.kind == "bill":
            bill = db.query(FinanceBill).filter(_ci(FinanceBill.name, line.name), FinanceBill.due_date == line.when,
                                                FinanceBill.status == "open").first()
            if bill is None:
                bill = FinanceBill(name=line.name, due_date=line.when)
                db.add(bill)
            bill.amount_cents, bill.scope, bill.category, bill.repeats = line.amount, line.scope, line.category, line.repeats
            _log(db, actor, "bill", f"{line.name} {plan.dollars(line.amount)} due {line.when} {line.scope}")
        elif line.kind == "owed":
            item = db.query(FinanceIncoming).filter(_ci(FinanceIncoming.source, line.name), FinanceIncoming.status == "expected").first()
            if item is None:
                item = FinanceIncoming(source=line.name)
                db.add(item)
            item.amount_cents, item.expected_date, item.scope = line.amount, line.when, line.scope
            _log(db, actor, "owed", f"{line.name} {plan.dollars(line.amount)} by {line.when} {line.scope}")
        elif line.kind == "paid":
            bill = (db.query(FinanceBill).filter(_ci(FinanceBill.name, line.name), FinanceBill.status == "open")
                    .order_by(FinanceBill.due_date).first())
            if bill is None:
                problems.append(f"No open bill named {line.name}.")
                continue
            _mark_paid(db, bill, today, actor)
        elif line.kind == "got":
            item = (db.query(FinanceIncoming).filter(_ci(FinanceIncoming.source, line.name), FinanceIncoming.status == "expected")
                    .first())
            if item is None:
                problems.append(f"Nothing expected from {line.name}.")
                continue
            item.status, item.received_on = "received", today
            _log(db, actor, "received", f"{item.source} {plan.dollars(item.amount_cents)}")
        db.flush()
        done += 1
    return done, problems


async def _save(request: Request, db: Session, work) -> RedirectResponse:
    """Runs one change as the owner. work(form, user, today) returns (notice, problems)."""
    user = _owner(request, db)
    form = await request.form()
    try:
        notice, problems = work(form, user, phoenix_today())
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return _back(form.get("window"), "", [NOT_SET_UP])
    return _back(form.get("window"), notice, problems)


@router.post("/paste")
async def paste(request: Request, db: Session = Depends(get_db)):
    def work(form, user, today):
        lines, problems = plan.parse_paste(str(form.get("figures", ""))[:20000], today)
        done, more = apply_lines(db, lines, today, user.username)
        return (f"Saved {done} figure{'s' if done != 1 else ''}." if done else ""), problems + more
    return await _save(request, db, work)


@router.post("/bills/{bill_id}/{action}")
async def bill_action(bill_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    if action not in ("paid", "skip"):
        raise HTTPException(404)

    def work(form, user, today):
        bill = db.get(FinanceBill, bill_id)
        if bill is None or bill.status != "open":
            return "", ["That bill was already closed."]
        if action == "paid":
            _mark_paid(db, bill, today, user.username)
            account = _move(db, form.get("account"), bill.scope, -bill.amount_cents, user.username)
            return f"Marked {bill.name} paid{f' from {account.name}' if account else ''}.", []
        bill.status = "skipped"
        _roll_forward(db, bill)
        _log(db, user.username, "bill_removed", f"{bill.name} due {bill.due_date}")
        return f"Took {bill.name} off the list.", []
    return await _save(request, db, work)


@router.post("/incoming/{item_id}/{action}")
async def incoming_action(item_id: int, action: str, request: Request, db: Session = Depends(get_db)):
    if action not in ("got", "drop"):
        raise HTTPException(404)

    def work(form, user, today):
        item = db.get(FinanceIncoming, item_id)
        if item is None or item.status != "expected":
            return "", ["That one was already closed."]
        item.status = "received" if action == "got" else "dropped"
        item.received_on = today if action == "got" else None
        _log(db, user.username, item.status, f"{item.source} {plan.dollars(item.amount_cents)}")
        if action == "got":
            account = _move(db, form.get("account"), item.scope, item.amount_cents, user.username)
            return f"Got {item.source}'s payment{f' into {account.name}' if account else ''}.", []
        return f"Took {item.source} off the list.", []
    return await _save(request, db, work)


@router.post("/add")
async def add(request: Request, db: Session = Depends(get_db)):
    """The tap-friendly form: builds one pasted line from the fields and saves it the same way."""
    def work(form, user, today):
        kind = str(form.get("kind", ""))
        fields = [kind, str(form.get("name", "")).replace("|", " "), str(form.get("amount", ""))]
        if kind in ("bill", "owed") and form.get("when"):
            fields.append(str(form.get("when")))
        fields.append(str(form.get("scope", "business")))
        if kind == "bill":
            fields += [str(form.get("category", "other")), str(form.get("repeats", "once"))]
        if kind == "balance" and str(form.get("held", "")).strip():
            fields.append("held " + str(form.get("held")))
        lines, problems = plan.parse_paste(" | ".join(fields), today)
        done, more = apply_lines(db, lines, today, user.username)
        return ("Saved." if done else ""), [p.replace("Line 1: ", "") for p in problems + more]
    return await _save(request, db, work)


@router.post("/goal")
async def goal(request: Request, db: Session = Depends(get_db)):
    def work(form, user, today):
        try:
            value = plan.cents(form.get("goal", "")) // 100
        except ValueError:
            return "", ["The daily goal has to be a dollar amount."]
        if not 0 < value <= 100000:
            return "", ["The daily goal has to be between $1 and $100,000."]
        settings_store.save(db, "finance_daily_goal", str(value), user.username)
        settings_store.clear_cache()
        return f"Daily goal set to ${value:,}.", []
    return await _save(request, db, work)
