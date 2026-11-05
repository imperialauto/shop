"""Owner-only, read-only finance landing view.

Phase one deliberately uses existing Repair Order payment records. It does NOT
treat invoice payments as an audited historical P&L or import external bank data.
Finance imports need a separate, idempotent database migration and source audit.
"""
import re
from pathlib import Path
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Invoice, Payment, RepairOrder

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def month_boundaries(period: str | None):
    """Return an inclusive start and exclusive end for a YYYY-MM month."""
    now = datetime.utcnow()
    if not period or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        year, month = now.year, now.month
    else:
        year, month = map(int, period.split("-"))
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end


@router.get("/", response_class=HTMLResponse)
def finance_home(
    request: Request,
    period: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        user = get_current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=302)
        raise
    require_admin(user)

    start, end = month_boundaries(period)
    amount, count = (
        db.query(func.coalesce(func.sum(Payment.amount), 0), func.count(Payment.id))
        .filter(Payment.paid_at >= start, Payment.paid_at < end)
        .one()
    )
    unpaid_total, unpaid_count = (
        db.query(func.coalesce(func.sum(Invoice.total), 0), func.count(Invoice.id))
        .filter(Invoice.status == "sent")
        .one()
    )
    payments = (
        db.query(Payment)
        .options(
            joinedload(Payment.invoice)
            .joinedload(Invoice.repair_order)
            .joinedload(RepairOrder.customer)
        )
        .filter(Payment.paid_at >= start, Payment.paid_at < end)
        .order_by(Payment.paid_at.desc())
        .limit(15)
        .all()
    )
    activity = []
    for payment in payments:
        invoice = payment.invoice
        ro = invoice.repair_order if invoice else None
        customer = ro.customer if ro else None
        display_name = (
            (f"{customer.first_name} {customer.last_name}").strip()
            if customer else "Customer not linked"
        )
        activity.append({
            "date": payment.paid_at,
            "amount": float(payment.amount or 0),
            "method": payment.method or "Unspecified",
            "customer": display_name,
            "invoice_id": payment.invoice_id,
        })

    return templates.TemplateResponse(
        "finance/home.html",
        {
            "request": request,
            "user": user,
            "period": start.strftime("%Y-%m"),
            "period_label": start.strftime("%B %Y"),
            "recorded_amount": float(amount or 0),
            "recorded_count": int(count or 0),
            "sent_invoice_amount": float(unpaid_total or 0),
            "sent_invoice_count": int(unpaid_count or 0),
            "activity": activity,
            "read_only": True,
        },
    )
