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
from app.appointments import PHOENIX
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.config import OWNER_PHONE
from app.dispatch import AUTO_POLICIES, LOCKED_POLICIES, POLICIES, apply_policies, assess, auto_reply, parse_owner_command, propose
from app.appointment_model import Appointment
from app.dispatch_model import DispatchAudit, DispatchEvent, DispatchItem, DispatchOutbound, DispatchPolicy, DispatchSync, OPEN_STATUSES
from app.models import Communication, Customer, RepairOrder, Vehicle
from app.routes.internal_messaging import _find_customer_by_phone, _normalize_phone, require_internal_key

router = APIRouter()
internal_router = APIRouter()

URGENCY_RANK = {"normal": 0, "urgent": 1, "safety": 2}
SUMMARY_LIMIT = 280


class IntakeMessage(BaseModel):
    conversationId: str = Field(min_length=1, max_length=64)
    messageId: str = Field(min_length=1, max_length=128)
    phone: str = Field(min_length=1, max_length=32)
    body: str = Field(default="", max_length=4000)
    contactName: str | None = Field(default=None, max_length=120)
    kind: str = Field(default="message", pattern="^(message|call)$")
    threadMessageId: str | None = Field(default=None, max_length=128)
    recovery: bool = False  # replayed by the catch-up job: never reply or act on it


class DraftInput(BaseModel):
    conversationId: str = Field(min_length=1, max_length=64)
    messageId: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=2000)


class OutboundResult(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    status: str = Field(pattern="^(sent|failed|skipped)$")


class SyncReport(BaseModel):
    source: str = Field(min_length=1, max_length=32)
    ok: bool
    detail: str = Field(default="", max_length=255)


def audit(db: Session, item_id: int | None, actor: str, action: str, detail: str | None = None):
    db.add(DispatchAudit(item_id=item_id, actor=actor, action=action, detail=detail))


def policy_modes(db: Session) -> dict[str, str]:
    return {p.key: p.mode for p in db.query(DispatchPolicy).all()}


def _record(db: Session, message: IntakeMessage, result) -> DispatchItem:
    customer = _find_customer_by_phone(db, message.phone)
    vehicle = None
    if customer and result.vin:
        vehicle = db.query(Vehicle).filter(Vehicle.customer_id == customer.id, Vehicle.vin == result.vin).first()

    item = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == message.conversationId,
        DispatchItem.status.in_(OPEN_STATUSES),
    ).first()
    earlier = json.loads(item.approval_reasons) if item else []
    reasons = sorted(set(earlier) | set(result.reasons) | ({"missed_call"} if message.kind == "call" else set()))
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
    item.contact_name = message.contactName or item.contact_name
    item.summary = message.body.strip()[:SUMMARY_LIMIT]
    item.thread_message_id = message.threadMessageId
    item.draft_reply = None  # any earlier draft answered an older message
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


def reserve(db: Session, key: str, kind: str, item_id: int | None = None) -> bool:
    """True once for each key. A lost race or a repeat returns False."""
    if db.query(DispatchOutbound).filter(DispatchOutbound.dedupe_key == key).first():
        return False
    db.add(DispatchOutbound(dedupe_key=key, kind=kind, item_id=item_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


def _auto_reply_for(db: Session, item: DispatchItem, message: IntakeMessage, result):
    if message.recovery or item.status != "routine" or item.urgency != "normal":
        return None
    has_vehicle = bool(item.vehicle_text) or (
        item.customer_id is not None
        and db.query(Vehicle).filter(Vehicle.customer_id == item.customer_id).first() is not None
    )
    choice = auto_reply(result, has_vehicle, policy_modes(db))
    if choice is None:
        return None
    template, text = choice
    # Hours can repeat a week later; the vehicle question is only asked once.
    period = datetime.now(PHOENIX).strftime("%G-%V") if template == "hours" else "once"
    key = f"auto:{message.conversationId}:{template}:{period}"
    if not reserve(db, key, "customer", item.id):
        return None
    audit(db, item.id, "system", "auto_reply_reserved", template)
    db.commit()
    return {"key": key, "text": text}


def _is_owner(phone: str) -> bool:
    return bool(OWNER_PHONE) and _normalize_phone(phone) == _normalize_phone(OWNER_PHONE)


def _open_approvals(db: Session) -> list[DispatchItem]:
    return db.query(DispatchItem).filter(DispatchItem.status == "needs_owner").order_by(DispatchItem.id).all()


def _label(db: Session, item: DispatchItem) -> str:
    customer = db.get(Customer, item.customer_id) if item.customer_id else None
    name = f"{customer.first_name or ''} {customer.last_name or ''}".strip() if customer else ""
    name = name or item.contact_name or ""
    vehicle = item.vehicle_text
    if not vehicle and item.vehicle_id:
        row = db.get(Vehicle, item.vehicle_id)
        vehicle = f"{row.year or ''} {row.make or ''} {row.model or ''}".strip() if row else None
    if vehicle and vehicle.casefold() in name.casefold():
        vehicle = None  # contact labels often already include the vehicle
    return " - ".join(part for part in (name, vehicle) if part) or item.customer_phone


def _snapshot_item(db: Session, item: DispatchItem) -> dict:
    return {"id": item.id, "who": _label(db, item), "phone": item.customer_phone, "type": item.category,
            "urgency": item.urgency, "lastMessage": item.summary[:200], "nextStep": item.proposed_action[:160],
            "draftReply": (item.draft_reply or "")[:200] or None,
            "updatedPhoenix": _utc(item.updated_at).astimezone(PHOENIX).strftime("%a %b %-d %-I:%M %p")}


def _today_activity(db: Session, start: datetime) -> dict:
    since = start.astimezone(timezone.utc)
    audits = db.query(DispatchAudit.action, DispatchAudit.detail).filter(DispatchAudit.created_at >= since).all()
    count = lambda action: sum(1 for row in audits if row.action == action)
    return {
        "customerMessagesAndCalls": count("message_received"),
        "newItems": db.query(DispatchItem).filter(DispatchItem.created_at >= since).count(),
        "textsSent": count("text_sent"),
        "textsFailed": count("text_failed"),
        "approvedByOwner": count("owner_approve") + count("owner_edit"),
        "declinedByOwner": count("owner_decline"),
        "collectedRevenue": "Not available yet: payments are not recorded in the app.",
    }


def _owner_snapshot(db: Session) -> dict:
    now = datetime.now(PHOENIX)
    def rows(status, limit):
        found = db.query(DispatchItem).filter(DispatchItem.status == status).order_by(DispatchItem.updated_at.desc()).limit(limit).all()
        return [_snapshot_item(db, item) for item in found]
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    booked = db.query(Appointment).filter(
        Appointment.status.in_(("tentative", "confirmed")),
        Appointment.starts_at >= start.astimezone(timezone.utc),
        Appointment.starts_at < (start + timedelta(days=2)).astimezone(timezone.utc),
    ).order_by(Appointment.starts_at).limit(20).all()
    return {
        "nowPhoenix": now.strftime("%a %b %-d %-I:%M %p"),
        "shop": "Open Mon-Fri 11-7, Sat 12-5, closed Sunday. Suite 508.",
        "waitingOnYou": rows("needs_owner", 15),
        "otherOpenMessages": rows("routine", 10),
        "approvedAwaitingFollowUp": rows("approved", 8),
        "appBookingsTodayAndTomorrow": [
            {"time": _utc(a.starts_at).astimezone(PHOENIX).strftime("%a %-I:%M %p"), "resource": a.resource_id, "status": a.status}
            for a in booked
        ],
        "todaySoFar": _today_activity(db, start),
        "notInThisSnapshot": "Google Calendar events, invoices, and website forms.",
    }


def _owner_command(db: Session, message: IntakeMessage) -> dict:
    command = parse_owner_command(message.body)
    if command is None:
        return {"ownerQuestion": message.body.strip()[:1000], "snapshot": _owner_snapshot(db)}
    decision, item_id = command[:2]
    waiting = _open_approvals(db)
    if decision == "list":
        if not waiting:
            return {"ownerReply": "Nothing is waiting on you."}
        return {"ownerReply": "\n".join(f"#{i.id} {_label(db, i)}: {i.category}" for i in waiting[:8])}
    if item_id is None:
        if len(waiting) != 1:
            return {"ownerReply": f"{len(waiting)} approvals are open. Reply with a number, like YES 12."}
        item_id = waiting[0].id
    item = db.query(DispatchItem).filter(DispatchItem.id == item_id).with_for_update().first()
    if item is None or item.status not in OPEN_STATUSES:
        return {"ownerReply": f"#{item_id} is not open."}
    alerted = db.query(DispatchOutbound).filter(
        DispatchOutbound.dedupe_key == f"alert:{item.id}:{item.last_message_id}",
        DispatchOutbound.status == "sent",
    ).first()
    if alerted is None:
        return {"ownerReply": f"#{item.id} has a newer customer message. Check the latest text before deciding."}
    text = command[2] if decision == "edit" else item.draft_reply if decision == "approve" else None
    item.status = "declined" if decision == "decline" else "approved"
    item.decided_at = item.updated_at = datetime.now().astimezone()
    audit(db, item.id, "owner_sms", f"owner_{decision}")
    db.commit()
    if decision == "decline":
        return {"ownerReply": f"Skipped #{item.id}. Nothing was sent."}
    if not text:
        return {"ownerReply": f"Approved #{item.id}. No draft to send, so reply in Quo. Next: {item.proposed_action}"[:300]}
    key = f"reply:{item.id}:{item.last_message_id}"
    if not reserve(db, key, "customer", item.id):
        return {"ownerReply": f"#{item.id} was already sent."}
    return {"customerReply": {"key": key, "itemId": item.id, "phone": item.customer_phone, "text": text,
                              "messageId": item.thread_message_id or item.last_message_id, "conversationId": item.quo_conversation_id}}


@internal_router.post("/intake")
def intake(message: IntakeMessage, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    if db.query(DispatchEvent).filter(DispatchEvent.quo_message_id == message.messageId).first():
        if message.contactName:  # lets recovery label items queued before contacts were read
            item = db.query(DispatchItem).filter(
                DispatchItem.quo_conversation_id == message.conversationId,
                DispatchItem.status.in_(OPEN_STATUSES),
                DispatchItem.contact_name.is_(None),
            ).first()
            if item:
                item.contact_name = message.contactName
                db.commit()
        return {"recorded": False, "duplicate": True}
    if _is_owner(message.phone):
        if message.recovery:
            return {"recorded": False, "duplicate": True, "owner": True}
        db.add(DispatchEvent(quo_message_id=message.messageId))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {"recorded": False, "duplicate": True, "owner": True}
        return {"recorded": True, "owner": True, **_owner_command(db, message)}
    result = assess(message.body)
    for attempt in (1, 2):  # a concurrent delivery can win the unique index once
        try:
            item = _record(db, message, result)
            return {"recorded": True, "itemId": item.id, "status": item.status,
                    "vehicleText": item.vehicle_text, "category": item.category,
                    "autoReply": _auto_reply_for(db, item, message, result)}
        except IntegrityError:
            db.rollback()
            if db.query(DispatchEvent).filter(DispatchEvent.quo_message_id == message.messageId).first():
                return {"recorded": False, "duplicate": True}
            if attempt == 2:
                raise


NUDGE_AFTER = {"needs_owner": timedelta(hours=2), "approved": timedelta(hours=24)}
TICK_LIMIT = 10
DAILY_HOUR = 10
DRAFT_GRACE = timedelta(seconds=90)
NUDGE_HOURS = range(10, 19)


def _alert_text(db: Session, item: DispatchItem) -> str:
    head = "URGENT" if item.urgency != "normal" else "Approval"
    summary = " ".join(item.summary.split())[:160]
    lines = [f"{head} #{item.id}: {_label(db, item)}", f"Customer said: {summary}"]
    if item.draft_reply:
        lines.append(f"Draft reply: {item.draft_reply[:320]}")
        lines.append(f"YES {item.id} sends it. NO {item.id} skips. EDIT {item.id} your text sends yours.")
    else:
        lines.append(f"Next: {item.proposed_action[:140]}")
        lines.append(f"Reply YES {item.id} or NO {item.id}")
    return "\n".join(lines)


def _daily_text(db: Session, now: datetime) -> str:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    appointments = db.query(Appointment).filter(
        Appointment.status.in_(("tentative", "confirmed")),
        Appointment.starts_at >= start.astimezone(timezone.utc),
        Appointment.starts_at < (start + timedelta(days=1)).astimezone(timezone.utc),
    ).order_by(Appointment.starts_at).all()
    times = ", ".join(_utc(a.starts_at).astimezone(PHOENIX).strftime("%-I:%M %p") for a in appointments[:6])
    waiting = _open_approvals(db)
    customers = db.query(DispatchItem).filter(DispatchItem.status == "approved").count()
    routine = db.query(DispatchItem).filter(DispatchItem.status == "routine").count()
    lines = [
        f"Today: {len(appointments)} booked" + (f" ({times})" if times else ""),
        f"Waiting on you: {len(waiting)}",
        f"Waiting on customers: {customers}",
        f"Other open messages: {routine}",
    ]
    lines += [f"#{i.id} {_label(db, i)}" for i in waiting[:4]]
    return "\n".join(lines)


def due_owner_messages(db: Session, now: datetime) -> list[dict]:
    """Everything owed to the owner right now. Failed texts come back next tick."""
    if not OWNER_PHONE:
        return []
    now = now.astimezone(PHOENIX)
    due: list[tuple[str, str, int | None, str]] = []
    for item in db.query(DispatchItem).filter(DispatchItem.status.in_(OPEN_STATUSES)).all():
        if item.status == "needs_owner" or item.urgency != "normal":
            # Give the draft a moment to arrive, except on safety issues.
            young = now - _utc(item.updated_at).astimezone(PHOENIX) < DRAFT_GRACE
            if item.draft_reply is None and item.urgency != "safety" and young:
                continue
            due.append((f"alert:{item.id}:{item.last_message_id}", "owner_alert", item.id, _alert_text(db, item)))
    if now.hour in NUDGE_HOURS:
        for item in db.query(DispatchItem).filter(DispatchItem.status.in_(tuple(NUDGE_AFTER))).all():
            if now - _utc(item.updated_at).astimezone(PHOENIX) >= NUDGE_AFTER[item.status]:
                what = "Still waiting on you" if item.status == "needs_owner" else "No customer follow-up yet"
                due.append((f"nudge:{item.id}:{now.date()}", "owner_nudge", item.id,
                            f"{what}: #{item.id} {_label(db, item)}. {item.proposed_action[:120]}"))
    if now.weekday() != 6 and DAILY_HOUR <= now.hour < DAILY_HOUR + 3:
        due.append((f"daily:{now.date()}", "owner_daily", None, _daily_text(db, now)))

    messages = []
    for key, kind, item_id, text in due:
        row = db.query(DispatchOutbound).filter(DispatchOutbound.dedupe_key == key).first()
        if row is None:
            if not reserve(db, key, kind, item_id):
                continue
        elif row.status == "failed":
            row.status = "reserved"
            db.commit()
        else:
            continue
        messages.append({"key": key, "text": text})
        if len(messages) >= TICK_LIMIT:
            break
    return messages


@internal_router.post("/tick")
def tick(db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    return {"ownerPhone": OWNER_PHONE or None, "messages": due_owner_messages(db, datetime.now(timezone.utc))}


@internal_router.post("/draft")
def save_draft(draft: DraftInput, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    """Keeps a draft only if it answers the conversation's latest message."""
    item = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == draft.conversationId,
        DispatchItem.status.in_(OPEN_STATUSES),
        DispatchItem.last_message_id == draft.messageId,
    ).first()
    if item is None:
        return {"recorded": False}
    item.draft_reply = draft.body.strip()
    audit(db, item.id, "system", "draft_saved")
    db.commit()
    return {"recorded": True, "itemId": item.id}


@internal_router.post("/outbound")
def outbound_result(result: OutboundResult, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    row = db.query(DispatchOutbound).filter(DispatchOutbound.dedupe_key == result.key).first()
    if row is None:
        raise HTTPException(404, "Unknown message.")
    row.status = result.status
    row.updated_at = datetime.now().astimezone()
    audit(db, row.item_id, "system", f"text_{result.status}", row.kind)
    db.commit()
    return {"recorded": True}


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
    for key in AUTO_POLICIES:
        mode = form.get(key, "auto")
        if mode not in ("auto", "off"):
            raise HTTPException(422, "Invalid policy mode.")
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
        "customer_name": (f"{customer.first_name or ''} {customer.last_name or ''}".strip() if customer else None) or item.contact_name,
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
                "dispatch_view": view, "dispatch_views": VIEWS, "dispatch_health": [], "dispatch_auto_policies": []}
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
        "dispatch_auto_policies": [
            {"key": key, "label": label, "mode": modes.get(key, "auto")} for key, label in AUTO_POLICIES.items()
        ],
    }
