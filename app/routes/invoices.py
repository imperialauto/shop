from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Invoice, LineItem, RepairOrder, Payment
from app import next_visit, referrals
from app.auth import get_current_user
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote
import math
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


# Every method the counter can record. The owner picks which show (Business settings → Payment methods).
METHOD_LABELS = {
    "zelle": "Zelle", "cash": "Cash", "apple_cash": "Apple Pay", "card": "Card (in person)",
    "quickbooks": "QuickBooks Payments (card)", "stripe": "Stripe", "check": "Check", "ach": "Bank transfer (ACH)",
    "paypal": "PayPal", "venmo": "Venmo", "cashapp": "Cash App", "snap_finance": "Snap Finance", "other": "Other (write it in Reference)",
}
DEFAULT_METHODS = ("quickbooks", "zelle", "apple_cash", "cash", "snap_finance", "other")  # owner, Nov 24
PAYMENT_METHODS = DEFAULT_METHODS  # kept for older imports


def enabled_methods() -> tuple[str, ...]:
    """The owner's chosen methods, in order. Falls back to the defaults on any problem."""
    from app import business_settings
    raw = business_settings.raw("payment_methods") or ""
    chosen = tuple(m for m in (x.strip().lower() for x in raw.split(",")) if m in METHOD_LABELS)
    return chosen or DEFAULT_METHODS
DUPLICATE_WINDOW = timedelta(minutes=2)


def _balance(invoice):
    paid = sum(p.amount for p in invoice.payments)
    return round((invoice.total or 0) - paid - (getattr(invoice, "waived_amount", 0) or 0), 2)


def money_error(amount):
    if not math.isfinite(amount) or amount <= 0:
        return "Enter an amount above zero."
    if round(amount, 2) != round(amount, 6):
        return "Amounts can only have two decimal places."
    return None


def payment_error(invoice, amount, method, reference, now, paid_at=None):
    """Why this payment cannot be recorded, or None when it is fine.
    paid_at is the day the money actually came in, when that was before today."""
    if invoice.status == "void":
        return "This invoice is void."
    if method not in enabled_methods():
        return "Choose a valid payment method."
    problem = money_error(amount)
    if problem:
        return problem
    balance = _balance(invoice)
    if balance <= 0:
        return "This invoice is already paid in full."
    if round(amount, 2) > balance:
        return f"That is more than the ${balance:.2f} balance."
    for payment in invoice.payments:
        if reference and payment.reference == reference:
            return "That reference was already recorded."
        if paid_at is None:
            recent = payment.paid_at and now - payment.paid_at < DUPLICATE_WINDOW
            if recent and payment.amount == round(amount, 2) and payment.method == method:
                return "That payment was just recorded. Wait two minutes if this is a second one."
        elif (payment.paid_at and _shop_day(payment.paid_at) == _shop_day(paid_at) and payment.amount == round(amount, 2)
              and payment.method == method and not reference):
            return "A payment like that is already recorded for that day. Add a reference if this is a second one."
    return None


SHOP_TZ = ZoneInfo("America/Phoenix")
OLDEST_BACKDATE = timedelta(days=366)


def _shop_day(naive_utc):
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(SHOP_TZ).date()


def paid_on_to_utc(paid_on: str, now: datetime):
    """("", None) for today, (naive UTC noon on that shop day, None) for an earlier day, or (None, error)."""
    paid_on = (paid_on or "").strip()
    if not paid_on:
        return None, None
    try:
        day = date.fromisoformat(paid_on)
    except ValueError:
        return None, "Enter the payment date as a date."
    today = _shop_day(now)
    if day > today:
        return None, "The payment date can't be in the future."
    if day == today:
        return None, None
    if today - day > OLDEST_BACKDATE:
        return None, "That payment date is more than a year ago."
    noon = datetime.combine(day, time(12, 0), tzinfo=SHOP_TZ)
    return noon.astimezone(timezone.utc).replace(tzinfo=None), None


def require_user(request, db):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


@router.get("/", response_class=HTMLResponse)
def list_invoices(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    show_void = request.query_params.get("void") == "1"
    return templates.TemplateResponse("invoices/list.html", {
        "request": request, "user": user, "show_void": show_void, **invoice_board(db, show_void)})


def _vehicle_label(vehicle) -> str:
    return " ".join(str(x) for x in (vehicle.year, vehicle.make, vehicle.model) if x) if vehicle else ""


def _row(invoice) -> dict:
    ro = invoice.repair_order
    customer = ro.customer if ro else None
    paid = invoice.paid_total
    balance = invoice.balance
    if invoice.status == "void":
        label = "Void"
    elif balance < 0:
        label = "Overpaid"  # more came in than the invoice total: worth a look
    elif balance == 0:
        label = "Paid" if invoice.status == "paid" else "Nothing owed, not closed"
    elif invoice.status == "draft":
        label = "Draft, not sent"
    elif paid > 0 or (invoice.waived_amount or 0) > 0:
        label = "Partly paid"
    elif invoice.status == "paid":
        label = "Marked paid, money still owed"  # shouldn't happen; shown so it gets looked at
    else:
        label = "Sent, unpaid"
    return {
        "invoice": invoice, "job_id": invoice.repair_order_id,
        "customer": " ".join(x for x in ((customer.first_name or ""), (customer.last_name or "")) if x) if customer else "",
        "vehicle": _vehicle_label(ro.vehicle if ro else None),
        "total": round(invoice.total or 0, 2), "paid": paid, "balance": balance, "label": label,
        "date": invoice.created_at,
    }


def invoice_board(db, show_void: bool = False) -> dict:
    """Every invoice, split by what is still owed, plus approved work on open jobs that has no invoice.
    Reads only: nothing is created or changed here."""
    from sqlalchemy.orm import joinedload
    from app import job_deposits
    from app.ro_status_policy import is_open
    from app.routes.repair_orders import RO_STATUSES
    status_labels = dict(RO_STATUSES)
    invoices = (db.query(Invoice)
                .options(joinedload(Invoice.payments),
                         joinedload(Invoice.repair_order).joinedload(RepairOrder.customer),
                         joinedload(Invoice.repair_order).joinedload(RepairOrder.vehicle))
                .order_by(Invoice.created_at.desc(), Invoice.id.desc()).all())
    unpaid, paid, void = [], [], []
    for invoice in invoices:
        row = _row(invoice)
        if invoice.status == "void":
            void.append(row)
        elif row["balance"] > 0 or invoice.status in ("draft", "sent"):
            unpaid.append(row)
        else:
            paid.append(row)
    unpaid.sort(key=lambda r: r["date"] or datetime.min)  # oldest money owed first

    invoiced_jobs = {inv.repair_order_id for inv in invoices if inv.status != "void"}
    not_invoiced = []
    jobs = (db.query(RepairOrder)
            .options(joinedload(RepairOrder.line_items), joinedload(RepairOrder.customer),
                     joinedload(RepairOrder.vehicle), joinedload(RepairOrder.deposits))
            .filter(RepairOrder.id.in_(db.query(LineItem.repair_order_id).filter(LineItem.approval == "approved")))
            .order_by(RepairOrder.id).all())
    for ro in jobs:
        if ro.id in invoiced_jobs or not is_open(ro):
            continue
        billable = [li for li in ro.line_items if li.billable]
        total = totals_for(billable)[3]
        deposits = job_deposits.unapplied_total(ro)
        not_invoiced.append({
            "job_id": ro.id, "status": status_labels.get(ro.status, (ro.status or "").replace("_", " ").capitalize()),
            "customer": " ".join(x for x in ((ro.customer.first_name or ""), (ro.customer.last_name or "")) if x) if ro.customer else "",
            "vehicle": _vehicle_label(ro.vehicle), "total": total, "deposits": deposits,
            "left": round(total - deposits, 2),
        })

    return {
        "unpaid": unpaid, "paid": paid, "void": void if show_void else [], "void_count": len(void),
        "owed": round(sum(max(r["balance"], 0) for r in unpaid), 2),
        "not_invoiced": not_invoiced,
        "not_invoiced_total": round(sum(max(r["left"], 0) for r in not_invoiced), 2),
    }


SHOP_TAX_RATE = 0.092  # 7915 N Glen Harbor Blvd: AZTaxes address lookup Nov 24 2026, Auto Repair: Maricopa 017 6.3 (state+county) + Glendale GE 017 2.9. The one rate everywhere.
# Parts only: separately stated repair labor is not taxed (TPR 93-31).


def totals_for(billable) -> tuple[float, float, float, float]:
    """(subtotal, tax rate, tax on parts, total), rounded to cents."""
    subtotal = round(sum(li.total for li in billable), 2)
    tax_amount = round(sum(li.total for li in billable if li.item_type == "part") * SHOP_TAX_RATE, 2)
    return subtotal, SHOP_TAX_RATE, tax_amount, round(subtotal + tax_amount, 2)


def refresh_draft(db, invoice, ro) -> None:
    """Re-total a draft invoice from the job's billable lines. Only drafts: a sent, signed or paid
    invoice is what the customer saw and stays as it is. Caller commits."""
    if invoice.status != "draft" or invoice.customer_signed_at:
        return
    invoice.subtotal, invoice.tax_rate, invoice.tax_amount, invoice.total = totals_for(
        [li for li in ro.line_items if li.billable])


@router.post("/create/{ro_id}")
def create_invoice(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # Lock the job so a double tap on Create invoice can't make two invoices.
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).with_for_update().first()
    if not ro:
        raise HTTPException(status_code=404)

    existing = db.query(Invoice).filter(Invoice.repair_order_id == ro_id, Invoice.status != "void").first()
    if existing:
        return RedirectResponse(f"/invoices/{existing.id}", status_code=303)
    if not any(li.billable for li in ro.line_items):
        return RedirectResponse(f"/ro/{ro_id}", status_code=303)
    next_visit.apply(db, ro, user.username)  # 10% off labor from their last paid job, when on
    referrals.apply_to_job(db, ro, user.username)  # $25 referral discount and store credit, when on
    billable = [li for li in ro.line_items if li.billable]

    subtotal, tax_rate, tax_amount, total = totals_for(billable)

    invoice = Invoice(
        repair_order_id=ro_id,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status="draft",
    )
    db.add(invoice)
    db.flush()
    from app import job_deposits
    job_deposits.apply_to_invoice(db, invoice)  # deposits already paid on the job count on the invoice
    db.commit()
    db.refresh(invoice)
    return RedirectResponse(f"/invoices/{invoice.id}", status_code=302)


@router.get("/{invoice_id}", response_class=HTMLResponse)
def view_invoice(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404)
    total_paid = sum(p.amount for p in invoice.payments)
    balance = _balance(invoice)
    return templates.TemplateResponse("invoices/detail.html", {
        "request": request, "user": user, "invoice": invoice,
        "total_paid": total_paid, "balance": balance,
        "today": _shop_day(datetime.utcnow()).isoformat(),
        "can_waive": user.role in ("owner", "admin"),
        "methods": [(m, METHOD_LABELS[m]) for m in enabled_methods()], "method_labels": METHOD_LABELS,
        "payment_error": request.query_params.get("error"),
    })


def _settle(invoice, picked_up=False):
    """Paid in full (payments plus any waived amount) closes the invoice; anything owed reopens it.
    Returns the job when this call marked it picked up."""
    if _balance(invoice) <= 0:
        invoice.status = "paid"
        # Paid in full at the counter: one tap also closes the job, but only once the work is complete.
        ro = invoice.repair_order
        if picked_up and ro is not None and ro.status == "complete":
            ro.status = "delivered"
            return ro
    elif invoice.status == "paid" or invoice.payments:
        invoice.status = "sent"
    return None


def _schedule_review(db, ro):
    """Owner's setting: the Google review text goes out the day after pickup (app/review_requests.py)."""
    if ro is None:
        return
    from app import review_requests
    try:
        with db.begin_nested():
            review_requests.schedule(db, ro)
    except Exception as exc:  # a review text must never stop a payment from saving
        print(f"review_schedule_unavailable job={ro.id} {type(exc).__name__}")


@router.post("/{invoice_id}/add-payment")
def add_payment(
    request: Request,
    invoice_id: int,
    db: Session = Depends(get_db),
    amount: float = Form(...),
    method: str = Form("zelle"),
    reference: str = Form(""),
    picked_up: str = Form(""),
    paid_on: str = Form(""),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    if not invoice:
        raise HTTPException(status_code=404)

    reference = reference.strip()
    now = datetime.utcnow()
    paid_at, error = paid_on_to_utc(paid_on, now)
    error = error or payment_error(invoice, amount, method, reference, now, paid_at)
    if error:
        return RedirectResponse(f"/invoices/{invoice_id}?error={quote(error)}", status_code=303)

    amount = round(amount, 2)
    payment = Payment(invoice_id=invoice_id, amount=amount, method=method, reference=reference or None)
    if paid_at is not None:
        payment.paid_at = paid_at
    invoice.payments.append(payment)
    ro = _settle(invoice, picked_up == "on")
    referrals.on_paid(db, invoice)  # the referrer earns $25 credit when the friend's job is paid
    _schedule_review(db, ro)
    db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=302)


@router.post("/{invoice_id}/waive")
def waive_balance(
    request: Request,
    invoice_id: int,
    db: Session = Depends(get_db),
    amount: float = Form(...),
    reason: str = Form(""),
    picked_up: str = Form(""),
):
    """The owner writes off what is left. Recorded apart from payments, so reports never count it as money in."""
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only the owner can waive a balance.")
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    if not invoice:
        raise HTTPException(status_code=404)
    balance = _balance(invoice)
    error = ("This invoice is void." if invoice.status == "void" else money_error(amount)
             or ("Nothing is left to waive." if balance <= 0 else None)
             or (f"That is more than the ${balance:.2f} balance." if round(amount, 2) > balance else None))
    if error:
        return RedirectResponse(f"/invoices/{invoice_id}?error={quote(error)}", status_code=303)
    invoice.waived_amount = round((invoice.waived_amount or 0) + round(amount, 2), 2)
    invoice.waived_reason = reason.strip()[:500] or None
    invoice.waived_by = user.username[:64]
    invoice.waived_at = datetime.now(timezone.utc)
    ro = _settle(invoice, picked_up == "on")
    referrals.on_paid(db, invoice)  # the referrer earns $25 credit when the friend's job is paid
    _schedule_review(db, ro)
    db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=302)


@router.post("/{invoice_id}/waive/undo")
def undo_waive(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in ("owner", "admin"):
        raise HTTPException(403, "Only the owner can change a waived balance.")
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    if not invoice:
        raise HTTPException(status_code=404)
    if invoice.status != "void" and invoice.waived_amount:
        invoice.waived_amount, invoice.waived_reason, invoice.waived_by, invoice.waived_at = 0.0, None, None, None
        _settle(invoice)
    db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=302)


@router.post("/{invoice_id}/mark-sent")
def mark_sent(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    # Only a draft becomes "sent"; this must never bring a paid or void invoice back to life.
    if invoice and invoice.status == "draft":
        invoice.status = "sent"
        db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=302)
