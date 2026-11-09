from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Invoice, RepairOrder, Payment
from app.auth import get_current_user
from datetime import datetime, timedelta
from urllib.parse import quote
import math
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


PAYMENT_METHODS = ("cash", "zelle", "card", "check", "ach", "paypal")
DUPLICATE_WINDOW = timedelta(minutes=2)


def payment_error(invoice, amount, method, reference, now):
    """Why this payment cannot be recorded, or None when it is fine."""
    if invoice.status == "void":
        return "This invoice is void."
    if method not in PAYMENT_METHODS:
        return "Choose a valid payment method."
    if not math.isfinite(amount) or amount <= 0:
        return "Enter an amount above zero."
    if round(amount, 2) != round(amount, 6):
        return "Amounts can only have two decimal places."
    balance = round(invoice.total - sum(p.amount for p in invoice.payments), 2)
    if balance <= 0:
        return "This invoice is already paid in full."
    if round(amount, 2) > balance:
        return f"That is more than the ${balance:.2f} balance."
    for payment in invoice.payments:
        if reference and payment.reference == reference:
            return "That reference was already recorded."
        recent = payment.paid_at and now - payment.paid_at < DUPLICATE_WINDOW
        if recent and payment.amount == round(amount, 2) and payment.method == method:
            return "That payment was just recorded. Wait two minutes if this is a second one."
    return None


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
    invoices = db.query(Invoice).order_by(Invoice.created_at.desc()).all()
    return templates.TemplateResponse("invoices/list.html", {"request": request, "user": user, "invoices": invoices})


SHOP_TAX_RATE = 0.092  # Glendale combined retail TPT: state 5.6, Maricopa 0.7, city 2.9 (ADOR table, June 2026).
# Parts only: separately stated repair labor is not taxed (TPR 93-31).


@router.post("/create/{ro_id}")
def create_invoice(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)

    existing = db.query(Invoice).filter(Invoice.repair_order_id == ro_id, Invoice.status != "void").first()
    if existing:
        return RedirectResponse(f"/invoices/{existing.id}", status_code=303)
    billable = [li for li in ro.line_items if li.billable]
    if not billable:
        return RedirectResponse(f"/ro/{ro_id}", status_code=303)

    subtotal = sum(li.total for li in billable)
    tax_rate = SHOP_TAX_RATE
    tax_amount = round(sum(li.total for li in billable if li.item_type == "part") * tax_rate, 2)
    total = subtotal + tax_amount

    invoice = Invoice(
        repair_order_id=ro_id,
        subtotal=subtotal,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        total=total,
        status="draft",
    )
    db.add(invoice)
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
    balance = invoice.total - total_paid
    return templates.TemplateResponse("invoices/detail.html", {
        "request": request, "user": user, "invoice": invoice,
        "total_paid": total_paid, "balance": balance,
        "payment_error": request.query_params.get("error"),
    })


@router.post("/{invoice_id}/add-payment")
def add_payment(
    request: Request,
    invoice_id: int,
    db: Session = Depends(get_db),
    amount: float = Form(...),
    method: str = Form("cash"),
    reference: str = Form(""),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).with_for_update().first()
    if not invoice:
        raise HTTPException(status_code=404)

    reference = reference.strip()
    error = payment_error(invoice, amount, method, reference, datetime.utcnow())
    if error:
        return RedirectResponse(f"/invoices/{invoice_id}?error={quote(error)}", status_code=303)

    amount = round(amount, 2)
    total_paid = sum(p.amount for p in invoice.payments) + amount  # before adding, so it counts once
    db.add(Payment(invoice_id=invoice_id, amount=amount, method=method, reference=reference or None))

    if total_paid >= round(invoice.total, 2):
        invoice.status = "paid"
    else:
        invoice.status = "sent"
    db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=302)


@router.post("/{invoice_id}/mark-sent")
def mark_sent(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if invoice:
        invoice.status = "sent"
        db.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=302)
