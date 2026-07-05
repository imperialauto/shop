from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import RepairOrder, Invoice
from datetime import datetime
import os

router = APIRouter()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
)


# ── Estimate / RO approval signing ─────────────────────────────────────────

@router.get("/ro/{token}", response_class=HTMLResponse)
def sign_ro_page(request: Request, token: str, db: Session = Depends(get_db)):
    ro = db.query(RepairOrder).filter(RepairOrder.public_token == token).first()
    if not ro:
        raise HTTPException(status_code=404, detail="Not found")
    subtotal = sum(li.total for li in ro.line_items)
    return templates.TemplateResponse("sign_ro.html", {
        "request": request, "ro": ro, "subtotal": subtotal,
    })


@router.post("/ro/{token}", response_class=HTMLResponse)
async def sign_ro_submit(request: Request, token: str, db: Session = Depends(get_db)):
    ro = db.query(RepairOrder).filter(RepairOrder.public_token == token).first()
    if not ro:
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    signature_data = form.get("signature_data", "").strip()
    approved_by = form.get("approved_by", "").strip()

    if not signature_data or signature_data in ("data:,", ""):
        subtotal = sum(li.total for li in ro.line_items)
        return templates.TemplateResponse("sign_ro.html", {
            "request": request, "ro": ro, "subtotal": subtotal,
            "error": "Please draw your signature before submitting.",
        })

    ro.signature_data = signature_data
    ro.approved_by = approved_by or "Customer"
    ro.signed_at = datetime.utcnow()
    # Auto-advance status from waiting_approval → in_progress
    if ro.status == "waiting_approval":
        ro.status = "in_progress"
    db.commit()

    return templates.TemplateResponse("sign_complete.html", {
        "request": request, "ro": ro, "doc_type": "estimate",
    })


# ── Invoice signing (at pickup / final bill) ────────────────────────────────

@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def sign_invoice_page(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse("sign_invoice.html", {
        "request": request, "invoice": invoice, "ro": invoice.repair_order,
    })


@router.post("/invoice/{invoice_id}", response_class=HTMLResponse)
async def sign_invoice_submit(request: Request, invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    signature_data = form.get("signature_data", "").strip()
    approved_by = form.get("approved_by", "").strip()

    if not signature_data or signature_data in ("data:,", ""):
        return templates.TemplateResponse("sign_invoice.html", {
            "request": request, "invoice": invoice, "ro": invoice.repair_order,
            "error": "Please draw your signature before submitting.",
        })

    invoice.customer_signature = signature_data
    invoice.customer_approved_by = approved_by or "Customer"
    invoice.customer_signed_at = datetime.utcnow()
    if invoice.status == "sent":
        invoice.status = "paid"
    db.commit()

    return templates.TemplateResponse("sign_complete.html", {
        "request": request, "ro": invoice.repair_order, "doc_type": "invoice",
    })
