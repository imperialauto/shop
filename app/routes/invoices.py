from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Invoice, RepairOrder, Payment
from app.auth import get_current_user
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


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


@router.post("/create/{ro_id}")
def create_invoice(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)

    subtotal = sum(li.total for li in ro.line_items)
    tax_rate = 0.0875
    tax_amount = sum(li.total for li in ro.line_items if li.taxable) * tax_rate
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
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404)

    payment = Payment(invoice_id=invoice_id, amount=amount, method=method, reference=reference or None)
    db.add(payment)

    total_paid = sum(p.amount for p in invoice.payments) + amount
    if total_paid >= invoice.total:
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
