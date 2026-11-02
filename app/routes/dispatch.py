"""Operations queue: records inbound work and owner decisions.

Nothing here sends a customer message. Approving records the decision; texts
still go out through the human-approved send path in the messaging service.
"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.dispatch import LOCKED_POLICIES, POLICIES, apply_policies, assess, propose
from app.dispatch_model import DispatchEvent, DispatchItem, DispatchPolicy, OPEN_STATUSES
from app.models import Vehicle
from app.routes.internal_messaging import _find_customer_by_phone, require_internal_key

router = APIRouter()
internal_router = APIRouter()

URGENCY_RANK = {"normal": 0, "urgent": 1, "safety": 2}
SUMMARY_LIMIT = 280


class IntakeMessage(BaseModel):
    conversationId: str = Field(min_length=1, max_length=64)
    messageId: str = Field(min_length=1, max_length=128)
    phone: str = Field(min_length=1, max_length=32)
    body: str = Field(default="", max_length=4000)


def policy_modes(db: Session) -> dict[str, str]:
    return {p.key: p.mode for p in db.query(DispatchPolicy).all()}


def _record(db: Session, message: IntakeMessage) -> DispatchItem:
    result = assess(message.body)
    customer = _find_customer_by_phone(db, message.phone)
    vehicle = None
    if customer and result.vin:
        vehicle = db.query(Vehicle).filter(Vehicle.customer_id == customer.id, Vehicle.vin == result.vin).first()

    item = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == message.conversationId,
        DispatchItem.status.in_(OPEN_STATUSES),
    ).first()
    earlier = json.loads(item.approval_reasons) if item else []
    reasons = sorted(set(earlier) | set(result.reasons))
    needs_owner = apply_policies(reasons, policy_modes(db))
    if item is None:
        item = DispatchItem(quo_conversation_id=message.conversationId, customer_phone=message.phone,
                            last_message_id=message.messageId, category=result.category,
                            urgency=result.urgency)
        db.add(item)
    else:
        if URGENCY_RANK[result.urgency] > URGENCY_RANK[item.urgency]:
            item.urgency = result.urgency
        if item.category == "general":
            item.category = result.category
    item.customer_id = customer.id if customer else item.customer_id
    item.vehicle_id = vehicle.id if vehicle else item.vehicle_id
    item.vehicle_text = result.vehicle_text or item.vehicle_text
    item.summary = message.body.strip()[:SUMMARY_LIMIT]
    item.proposed_action = propose(item.category, item.urgency, list(result.missing))
    item.approval_reasons = json.dumps(reasons)
    item.status = "needs_owner" if needs_owner or item.urgency == "safety" else "routine"
    item.last_message_id = message.messageId
    item.updated_at = datetime.now().astimezone()
    db.flush()
    db.add(DispatchEvent(quo_message_id=message.messageId, item_id=item.id))
    db.commit()
    return item


@internal_router.post("/intake")
def intake(message: IntakeMessage, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    if db.query(DispatchEvent).filter(DispatchEvent.quo_message_id == message.messageId).first():
        return {"recorded": False, "duplicate": True}
    for attempt in (1, 2):  # a concurrent delivery can win the unique index once
        try:
            item = _record(db, message)
            return {"recorded": True, "itemId": item.id, "status": item.status}
        except IntegrityError:
            db.rollback()
            if db.query(DispatchEvent).filter(DispatchEvent.quo_message_id == message.messageId).first():
                return {"recorded": False, "duplicate": True}
            if attempt == 2:
                raise


@router.post("/{item_id}/decide")
def decide(
    item_id: int,
    request: Request,
    decision: str = Form(...),
    expected_message_id: str = Form(...),
    proposed_action: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if decision not in ("approve", "decline"):
        raise HTTPException(422, "Decision must be approve or decline.")
    item = db.query(DispatchItem).filter(DispatchItem.id == item_id).with_for_update().first()
    if item is None:
        raise HTTPException(404, "Item not found.")
    if item.status not in OPEN_STATUSES:
        raise HTTPException(409, "This item was already decided.")
    if item.last_message_id != expected_message_id:
        raise HTTPException(409, "A newer customer message arrived. Review it before deciding.")
    if proposed_action.strip():
        item.proposed_action = proposed_action.strip()[:2000]
    item.status = "approved" if decision == "approve" else "declined"
    item.decided_by = user.id
    item.decided_at = datetime.now().astimezone()
    item.updated_at = item.decided_at
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/policies")
async def save_policies(request: Request, db: Session = Depends(get_db)):
    require_admin(get_current_user(request, db))
    form = await request.form()
    for key in POLICIES:
        mode = form.get(key, "approve")
        if mode not in ("approve", "notify"):
            raise HTTPException(422, "Invalid policy mode.")
        if key in LOCKED_POLICIES:
            mode = "approve"
        policy = db.get(DispatchPolicy, key)
        if policy is None:
            db.add(DispatchPolicy(key=key, mode=mode))
        else:
            policy.mode = mode
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


def dashboard_context(db: Session) -> dict:
    """Keeps the dashboard working if the migration has not been applied yet."""
    try:
        items = db.query(DispatchItem).filter(DispatchItem.status.in_(OPEN_STATUSES)).all()
        modes = policy_modes(db)
    except SQLAlchemyError:
        db.rollback()
        return {"dispatch_ready": False, "dispatch_items": [], "dispatch_policies": []}
    items.sort(key=lambda i: (i.status != "needs_owner", -URGENCY_RANK[i.urgency], -i.updated_at.timestamp()))
    return {
        "dispatch_ready": True,
        "dispatch_items": [
            {"item": item, "reasons": [POLICIES.get(r, r) for r in json.loads(item.approval_reasons)]}
            for item in items[:25]
        ],
        "dispatch_policies": [
            {"key": key, "label": label, "mode": modes.get(key, "approve"), "locked": key in LOCKED_POLICIES}
            for key, label in POLICIES.items()
        ],
    }
