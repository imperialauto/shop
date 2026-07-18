# ─────────────────────────────────────────────────────────
# Internal API for the standalone Node messaging module (the "shop"
# Railway service) to read repair-order/customer context and to generate
# signing links, WITHOUT ever sending a message itself.
#
# Every customer-facing send still goes through the messaging module's own
# human-approval flow (POST /api/messages/conversations/:id/send there) —
# these endpoints only return data (context, links, suggested text). That
# keeps a single approval gate for all outbound SMS instead of two.
#
# Auth: shared-secret header, not a user session — this is service-to-
# service traffic on Railway's private network, not a browser client.
#   X-Internal-Key: <INTERNAL_API_KEY>
#
# Mounted in main.py:
#   app.include_router(internal_messaging.router, prefix="/internal", tags=["internal"])
# ─────────────────────────────────────────────────────────

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from datetime import datetime
import os
import secrets

from app.database import get_db
from app.models import Customer, Vehicle, RepairOrder, Invoice, Communication

router = APIRouter()

INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY", "")

# Public base URL customers hit for signing links / status pages. Falls back
# to the known production URL if not set so this doesn't silently break.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://web-production-94989.up.railway.app")


def require_internal_key(x_internal_key: str | None = Header(default=None)):
    if not INTERNAL_API_KEY:
        # Fail closed — an internal API with real customer/financial data
        # must never run wide open just because the env var wasn't set.
        raise HTTPException(status_code=500, detail="INTERNAL_API_KEY not configured on this service")
    if not x_internal_key or x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Key header")
    return True


def _normalize_phone(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _find_customer_by_phone(db: Session, phone: str) -> Customer | None:
    """Matches loosely on the last 10 digits since staff-entered phone
    numbers in this app aren't guaranteed to be strict E.164, unlike the
    numbers Quo sends."""
    target = _normalize_phone(phone)
    if not target:
        return None
    candidates = db.query(Customer).filter(Customer.phone.isnot(None)).all()
    for c in candidates:
        if _normalize_phone(c.phone) == target:
            return c
    return None


@router.get("/messaging/context")
def get_messaging_context(
    phone: str = Query(..., description="Customer phone number, any format"),
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
):
    """
    Job context for AI draft generation: customer name, vehicle(s), the most
    relevant open repair order (status/concern/promised date), and an open
    invoice if one exists. Returns {"found": false} rather than 404 so the
    caller can degrade gracefully (draft without job context).
    """
    customer = _find_customer_by_phone(db, phone)
    if not customer:
        return {"found": False}

    vehicles = db.query(Vehicle).filter(Vehicle.customer_id == customer.id).all()

    open_ro = (
        db.query(RepairOrder)
        .filter(RepairOrder.customer_id == customer.id, RepairOrder.status != "delivered")
        .order_by(RepairOrder.created_at.desc())
        .first()
    )

    open_invoice = None
    if open_ro:
        open_invoice = (
            db.query(Invoice)
            .filter(Invoice.repair_order_id == open_ro.id, Invoice.status != "void")
            .order_by(Invoice.created_at.desc())
            .first()
        )

    result = {
        "found": True,
        "customer": {
            "id": customer.id,
            "name": f"{customer.first_name} {customer.last_name}".strip(),
            "customerType": customer.customer_type,
        },
        "vehicles": [
            {
                "id": v.id,
                "year": v.year,
                "make": v.make,
                "model": v.model,
                "engine": v.engine,
            }
            for v in vehicles
        ],
        "openRepairOrder": None,
        "openInvoice": None,
    }

    if open_ro:
        result["openRepairOrder"] = {
            "id": open_ro.id,
            "status": open_ro.status,
            "concern": open_ro.concern,
            "promisedDate": open_ro.promised_date.isoformat() if open_ro.promised_date else None,
            "signedAt": open_ro.signed_at.isoformat() if open_ro.signed_at else None,
            "hasSigningLink": bool(open_ro.public_token),
            "signingUrl": (
                f"{PUBLIC_BASE_URL}/sign/ro/{open_ro.public_token}" if open_ro.public_token else None
            ),
        }

    if open_invoice:
        result["openInvoice"] = {
            "id": open_invoice.id,
            "status": open_invoice.status,
            "total": open_invoice.total,
            "signedAt": (
                open_invoice.customer_signed_at.isoformat() if open_invoice.customer_signed_at else None
            ),
        }

    return result


@router.post("/messaging/repair-orders/{ro_id}/signing-link")
def create_or_get_signing_link(
    ro_id: int,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
):
    """
    Ensures the repair order has a public_token (creating one if it doesn't
    — same secrets.token_urlsafe scheme used everywhere else in this app)
    and returns the signing URL plus a suggested SMS body. Does NOT send
    anything — the messaging module stages this as a PENDING draft that
    still needs human approval before it goes out.
    """
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404, detail="Repair order not found")

    if not ro.public_token:
        ro.public_token = secrets.token_urlsafe(16)
        db.commit()
        db.refresh(ro)

    signing_url = f"{PUBLIC_BASE_URL}/sign/ro/{ro.public_token}"
    vehicle = ro.vehicle
    vehicle_label = f"{vehicle.year} {vehicle.make} {vehicle.model}".strip() if vehicle else "your vehicle"

    suggested_text = (
        f"Hi {ro.customer.first_name}, here's the estimate for {vehicle_label}: {signing_url} "
        f"— take a look and sign whenever you get a chance. Let us know if you have questions!"
    )

    return {
        "repairOrderId": ro.id,
        "signingUrl": signing_url,
        "suggestedText": suggested_text,
        "status": ro.status,
    }


@router.post("/messaging/communications")
def log_communication(
    payload: dict,
    db: Session = Depends(get_db),
    _auth: bool = Depends(require_internal_key),
):
    """
    Mirrors a Quo SMS (either direction) into this app's Communication log
    so it shows up natively on the customer's repair-order detail page,
    right alongside line items and invoices — no separate screen to check.

    Body: { "phone": "+1...", "direction": "inbound"|"outbound",
            "body": "...", "externalId": "quo message id" }
    Best-effort only: if there's no matching customer yet (e.g. a brand new
    lead who texted in before ever becoming a customer record), this is a
    no-op rather than an error — the messaging module's own history is the
    source of truth either way.
    """
    phone = payload.get("phone", "")
    direction = payload.get("direction", "inbound")
    body = payload.get("body", "")
    external_id = payload.get("externalId")

    customer = _find_customer_by_phone(db, phone)
    if not customer:
        return {"logged": False, "reason": "no matching customer"}

    open_ro = (
        db.query(RepairOrder)
        .filter(RepairOrder.customer_id == customer.id, RepairOrder.status != "delivered")
        .order_by(RepairOrder.created_at.desc())
        .first()
    )

    comm = Communication(
        repair_order_id=open_ro.id if open_ro else None,
        customer_id=customer.id,
        direction=direction,
        channel="sms",
        body=body,
        external_id=external_id,
    )
    db.add(comm)
    db.commit()

    return {"logged": True, "repairOrderId": open_ro.id if open_ro else None}
