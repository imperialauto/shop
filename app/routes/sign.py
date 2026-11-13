from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import RepairOrder, Invoice
from datetime import datetime, timezone

from app import approval_links, website_alerts
import os
import secrets
from app.security_controls import link_expired

router = APIRouter()
# Customer pages reached by a private link: never cached or indexed.
PRIVATE_PAGE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
)


# ── Estimate / RO approval signing ─────────────────────────────────────────

def _sign_page(request, ro, error=None, status=200):
    pending = [li for li in ro.line_items if li.approval == "pending"]
    subtotal = sum(li.total for li in ro.line_items if li.approval in ("approved", "pending"))
    return templates.TemplateResponse("sign_ro.html", {
        "request": request, "ro": ro, "subtotal": subtotal, "pending": pending, "error": error,
    }, status_code=status, headers=PRIVATE_PAGE)


@router.get("/ro/{token}", response_class=HTMLResponse)
def sign_ro_page(request: Request, token: str, db: Session = Depends(get_db)):
    ro = db.query(RepairOrder).filter(RepairOrder.public_token == token).first()
    if not ro or link_expired(ro):
        raise HTTPException(status_code=404, detail="Not found")
    return _sign_page(request, ro)


@router.post("/ro/{token}", response_class=HTMLResponse)
async def sign_ro_submit(request: Request, token: str, db: Session = Depends(get_db)):
    ro = db.query(RepairOrder).filter(RepairOrder.public_token == token).first()
    if not ro or link_expired(ro):
        raise HTTPException(status_code=404, detail="Not found")

    form = await request.form()
    signature_data = form.get("signature_data", "").strip()
    approved_by = form.get("approved_by", "").strip()

    pending = [li for li in ro.line_items if li.approval == "pending"]
    if not pending:
        if ro.signed_at:
            return _sign_page(request, ro)  # already decided
        return _sign_page(request, ro, "No estimate is ready for approval.", 422)
    decisions = {li.id: form.get(f"line_{li.id}") for li in pending}
    if any(choice not in ("approved", "declined") for choice in decisions.values()):
        return _sign_page(request, ro, "Choose Approve or Decline for each item.", 422)
    if not signature_data or signature_data in ("data:,", ""):
        return _sign_page(request, ro, "Please draw your signature before submitting.", 422)

    now = datetime.now(timezone.utc)
    for li in pending:
        li.approval, li.decided_at = decisions[li.id], now
    ro.signature_data = signature_data
    ro.approved_by = (approved_by or "Customer")[:128]
    ro.signed_at = datetime.utcnow()
    if pending:
        if ro.status == "waiting_approval":
            ro.status = "approval_received"
        website_alerts.queue_decision(db, ro, pending)
        approval_links.close_for(db, ro)
    # A signature records authorization; starting repair work is a separate staff decision.
    db.commit()

    return templates.TemplateResponse("sign_complete.html", {
        "request": request, "ro": ro, "doc_type": "estimate",
    }, headers=PRIVATE_PAGE)


# ── Invoice signing (at pickup / final bill) ────────────────────────────────

@router.get("/invoice/{invoice_id}", response_class=HTMLResponse)
def sign_invoice_page(request: Request, invoice_id: int, token: str = "", db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if (
        not invoice
        or invoice.status not in ("sent", "paid")
        or len(token) < 16
        or not invoice.repair_order.public_token
        or not secrets.compare_digest(invoice.repair_order.public_token, token)
        or link_expired(invoice.repair_order)
    ):
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse("sign_invoice.html", {
        "request": request, "invoice": invoice, "ro": invoice.repair_order,
    }, headers=PRIVATE_PAGE)


@router.post("/invoice/{invoice_id}", response_class=HTMLResponse)
async def sign_invoice_submit(request: Request, invoice_id: int, token: str = "", db: Session = Depends(get_db)):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if (
        not invoice
        or invoice.status not in ("sent", "paid")
        or len(token) < 16
        or not invoice.repair_order.public_token
        or not secrets.compare_digest(invoice.repair_order.public_token, token)
        or link_expired(invoice.repair_order)
    ):
        raise HTTPException(status_code=404, detail="Not found")

    if invoice.customer_signed_at or invoice.status == "paid":
        return RedirectResponse(f"/sign/invoice/{invoice_id}?token={token}", status_code=303)

    form = await request.form()
    signature_data = form.get("signature_data", "").strip()
    approved_by = form.get("approved_by", "").strip()

    if not signature_data or signature_data in ("data:,", ""):
        return templates.TemplateResponse("sign_invoice.html", {
            "request": request, "invoice": invoice, "ro": invoice.repair_order,
            "error": "Please draw your signature before submitting.",
        }, headers=PRIVATE_PAGE)

    invoice.customer_signature = signature_data
    invoice.customer_approved_by = approved_by or "Customer"
    invoice.customer_signed_at = datetime.utcnow()
    # Signing acknowledges the document; only actual payment records can settle it.
    db.commit()

    return templates.TemplateResponse("sign_complete.html", {
        "request": request, "ro": invoice.repair_order, "doc_type": "invoice",
    }, headers=PRIVATE_PAGE)

