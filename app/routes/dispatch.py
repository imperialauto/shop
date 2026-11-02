"""Operations queue: records inbound work and owner decisions.

Nothing here sends a customer message. Approving records the decision; texts
still go out through the human-approved send path in the messaging service.
"""
import json
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.dispatch import LOCKED_POLICIES, POLICIES, apply_policies, assess, propose
from app.appointment_model import Appointment
from app.dispatch_model import DispatchAudit, DispatchEvent, DispatchItem, DispatchPolicy, DispatchSync, OPEN_STATUSES
from app.models import Communication, Customer, RepairOrder, Vehicle
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


class SyncReport(BaseModel):
    source: str = Field(min_length=1, max_length=32)
    ok: bool
    detail: str = Field(default="", max_length=255)


def audit(db: Session, item_id: int | None, actor: str, action: str, detail: str | None = None):
    db.add(DispatchAudit(item_id=item_id, actor=actor, action=action, detail=detail))


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
    audit(db, item.id, "system", "message_received", f"{item.category}/{item.urgency}/{item.status}")
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


@internal_router.post("/sync")
def sync_report(report: SyncReport, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    row = db.get(DispatchSync, report.source)
    if row is None:
        row = DispatchSync(source=report.source, consecutive_failures=0)
        db.add(row)
    now = datetime.now(timezone.utc)
    if report.ok:
        row.last_success_at, row.consecutive_failures = now, 0
    else:
        row.last_failure_at, row.consecutive_failures = now, (row.consecutive_failures or 0) + 1
    row.last_detail = report.detail
    db.commit()
    return {"recorded": True}


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
    audit(db, item.id, user.username, f"owner_{decision}")
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/{item_id}/close")
def close(item_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    item = db.query(DispatchItem).filter(DispatchItem.id == item_id).with_for_update().first()
    if item is None:
        raise HTTPException(404, "Item not found.")
    if item.status != "approved":
        raise HTTPException(409, "Only approved items can be closed.")
    item.status = "closed"
    item.updated_at = datetime.now().astimezone()
    audit(db, item.id, user.username, "owner_close")
    db.commit()
    return RedirectResponse("/dashboard?view=customer", status_code=303)


@router.post("/policies")
async def save_policies(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    require_admin(user)
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
        audit(db, None, user.username, "policy_saved", f"{key}={mode}")
    db.commit()
    return RedirectResponse("/dashboard", status_code=303)


VIEWS = {
    "open": "All open", "urgent": "Urgent", "approval": "Awaiting approval",
    "customer": "Awaiting customer", "scheduled": "Scheduled", "completed": "Completed",
}
SYNC_STALE_AFTER = timedelta(minutes=45)
ESCALATE_AFTER_FAILURES = 3


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _scheduled_customers(db: Session) -> dict[int, datetime]:
    rows = db.query(RepairOrder.customer_id, Appointment.starts_at).join(
        Appointment, Appointment.repair_order_id == RepairOrder.id,
    ).filter(
        Appointment.status.in_(("tentative", "confirmed")),
        Appointment.starts_at > datetime.now(timezone.utc),
    ).order_by(Appointment.starts_at.desc()).all()
    return dict(rows)  # earliest upcoming wins: rows run latest first


def _select(db: Session, view: str, scheduled: dict[int, datetime]) -> list[DispatchItem]:
    query = db.query(DispatchItem)
    if view == "completed":
        return query.filter(DispatchItem.status.in_(("declined", "closed"))).order_by(DispatchItem.updated_at.desc()).limit(25).all()
    if view == "customer":
        return query.filter(DispatchItem.status == "approved").order_by(DispatchItem.updated_at.desc()).limit(25).all()
    if view == "scheduled":
        return query.filter(DispatchItem.customer_id.in_(list(scheduled)), DispatchItem.status.in_(OPEN_STATUSES + ("approved",))).limit(25).all()
    items = query.filter(DispatchItem.status.in_(OPEN_STATUSES)).all()
    if view == "urgent":
        items = [i for i in items if i.urgency != "normal"]
    elif view == "approval":
        items = [i for i in items if i.status == "needs_owner"]
    items.sort(key=lambda i: (i.status != "needs_owner", -URGENCY_RANK[i.urgency], -i.updated_at.timestamp()))
    return items[:25]


def _card(db: Session, item: DispatchItem, scheduled: dict[int, datetime]) -> dict:
    customer = db.get(Customer, item.customer_id) if item.customer_id else None
    history, open_ro, estimate = [], None, None
    if customer:
        history = db.query(Communication).filter(Communication.customer_id == customer.id).order_by(
            Communication.created_at.desc()).limit(3).all()
        open_ro = db.query(RepairOrder).filter(
            RepairOrder.customer_id == customer.id, RepairOrder.status != "delivered",
        ).order_by(RepairOrder.created_at.desc()).first()
        if open_ro and open_ro.line_items:
            estimate = sum(line.quantity * line.unit_price for line in open_ro.line_items)
    return {
        "item": item,
        "customer_name": f"{customer.first_name or ''} {customer.last_name or ''}".strip() if customer else None,
        "reasons": [POLICIES.get(r, r) for r in json.loads(item.approval_reasons)],
        "history": history,
        "open_ro": open_ro,
        "estimate": estimate,
        "next_appointment": scheduled.get(item.customer_id),
    }


def _sync_health(db: Session) -> list[dict]:
    now = datetime.now(timezone.utc)
    health = []
    for row in db.query(DispatchSync).order_by(DispatchSync.source).all():
        success = _utc(row.last_success_at)
        failing = (row.consecutive_failures or 0) >= ESCALATE_AFTER_FAILURES
        stale = success is None or now - success > SYNC_STALE_AFTER
        health.append({"source": row.source, "last_success_at": success, "problem": failing or stale,
                       "failures": row.consecutive_failures or 0})
    return health


def dashboard_context(db: Session, view: str = "open") -> dict:
    """Keeps the dashboard working if the migration has not been applied yet."""
    view = view if view in VIEWS else "open"
    try:
        modes = policy_modes(db)
        scheduled = _scheduled_customers(db)
        items = _select(db, view, scheduled)
        cards = [_card(db, item, scheduled) for item in items]
        health = _sync_health(db)
    except SQLAlchemyError:
        db.rollback()
        return {"dispatch_ready": False, "dispatch_items": [], "dispatch_policies": [],
                "dispatch_view": view, "dispatch_views": VIEWS, "dispatch_health": []}
    return {
        "dispatch_ready": True,
        "dispatch_items": cards,
        "dispatch_view": view,
        "dispatch_views": VIEWS,
        "dispatch_health": health,
        "dispatch_policies": [
            {"key": key, "label": label, "mode": modes.get(key, "approve"), "locked": key in LOCKED_POLICIES}
            for key, label in POLICIES.items()
        ],
    }
