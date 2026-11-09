"""Operations queue: records inbound work and owner decisions.

Nothing here sends a customer message. Approving records the decision; texts
still go out through the human-approved send path in the messaging service.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from app import calendar_feed, pricing
from app.appointments import PHOENIX
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.config import OWNER_PHONE
from app.dispatch import AUTO_POLICIES, LOCKED_POLICIES, MAKES, POLICIES, PROMISE, apply_policies, assess, auto_reply, is_reaction, parse_owner_command, propose
from app.appointment_model import Appointment
from app import alert_rules, approval_links, name_finder, next_action, quo_import, website_alerts
from app.sending_pause import is_paused, pause, resume
from app.dispatch_model import DispatchAudit, DispatchEvent, DispatchItem, DispatchOutbound, DispatchPolicy, DispatchSync, OPEN_STATUSES
from app.models import Communication, Customer, LineItem, PartsOrder, RepairOrder, Vehicle
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
    callOutcome: str | None = Field(default=None, pattern="^(answered|missed|voicemail|unknown)$")
    threadMessageId: str | None = Field(default=None, max_length=128)
    recovery: bool = False  # replayed by the catch-up job: never reply or act on it
    autoRepliesOn: bool = False
    knownCustomer: bool = False  # the conversation already has earlier messages


class DraftInput(BaseModel):
    conversationId: str = Field(min_length=1, max_length=64)
    messageId: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=2000)
    note: str | None = Field(default=None, max_length=300)


class OwnerAction(BaseModel):
    action: str = Field(pattern="^(propose|done|later|part_cost|quote_part|prepare_link)$")
    itemId: int | None = None
    text: str | None = Field(default=None, max_length=1500)
    hours: int | None = Field(default=None, ge=1, le=168)
    # Only part_cost uses these; it never writes a billable estimate line.
    ownerPhone: str | None = Field(default=None, max_length=32)
    ownerMessageId: str | None = Field(default=None, max_length=128)
    partDescription: str | None = Field(default=None, max_length=120)
    unitCost: float | None = Field(default=None, gt=0, le=50000, allow_inf_nan=False)
    eta: str | None = Field(default=None, max_length=64)
    evidence: str | None = Field(default=None, max_length=400)
    roId: int | None = Field(default=None, ge=1)
    partPrice: float | None = Field(default=None, gt=0, le=50000, allow_inf_nan=False)
    laborHours: float | None = Field(default=None, ge=0, le=40, allow_inf_nan=False)
    rateType: str | None = Field(default=None, pattern="^(shop|mobile|diesel|cspr)$")
    customerMessageId: str | None = Field(default=None, min_length=1, max_length=128)


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


def _auto_choice(db: Session, message: IntakeMessage, result, has_vehicle: bool):
    choice = auto_reply(result, has_vehicle, policy_modes(db))
    if choice and choice[0] == "vehicle" and message.knownCustomer:
        return None  # only a brand-new lead gets asked what they drive
    return choice


def _auto_key(conversation_id: str, template: str) -> str:
    # Hours can repeat a week later; the vehicle question is only asked once.
    period = datetime.now(PHOENIX).strftime("%G-%V") if template == "hours" else "once"
    return f"auto:{conversation_id}:{template}:{period}"


def _rule_answers(db: Session, message: IntakeMessage, result, item, customer) -> bool:
    """True when an automatic reply will really cover this message."""
    if not message.autoRepliesOn or message.recovery:
        return False
    has_vehicle = bool(result.vehicle_text or (item and item.vehicle_text)) or (
        customer is not None and db.query(Vehicle).filter(Vehicle.customer_id == customer.id).first() is not None
    )
    choice = _auto_choice(db, message, result, has_vehicle)
    if choice is None:
        return False
    key = _auto_key(message.conversationId, choice[0])
    return db.query(DispatchOutbound).filter(DispatchOutbound.dedupe_key == key).first() is None


def _record(db: Session, message: IntakeMessage, result) -> DispatchItem:
    customer = _find_customer_by_phone(db, message.phone)
    if customer is None:
        # A new number starts its customer file right away, so its history collects in one place.
        customer = quo_import.auto_create(db, message.phone, message.contactName)
    elif name_finder.unnamed(customer):
        customer.name_checked_at = None  # they may have just said who they are
    vehicle = None
    if customer and result.vin:
        vehicle = db.query(Vehicle).filter(Vehicle.customer_id == customer.id, Vehicle.vin == result.vin).first()

    item = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == message.conversationId,
        DispatchItem.status.in_(OPEN_STATUSES),
    ).first()
    earlier = json.loads(item.approval_reasons) if item else []
    # A Sona-answered call is not a missed call. Older senders omit callOutcome;
    # keep owner review for them without incorrectly labeling them as missed.
    call_reasons = set()
    if message.kind == "call":
        call_reasons.add("missed_call" if message.callOutcome in ("missed", "voicemail") else "customer_reply")
    reasons = sorted(set(earlier) | set(result.reasons) | call_reasons)
    needs_owner = apply_policies(reasons, policy_modes(db))
    if message.kind == "message" and not needs_owner and not _rule_answers(db, message, result, item, customer):
        reasons = sorted(set(reasons) | {"customer_reply"})  # nothing goes quiet just because no rule fits
        needs_owner = apply_policies(reasons, policy_modes(db))
    if item is None:
        for answered in db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == message.conversationId,
                                                      DispatchItem.status == "approved").all():
            answered.status = "closed"  # the new message carries the conversation forward
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
    # The service lead is independent of this short-lived communication task.
    # If the feature has not been migrated and enabled, existing intake is unchanged.
    if os.getenv("SERVICE_LEADS_ENABLED", "").strip().lower() == "true":
        from app.lead_lifecycle import intake_from_quo
        intake_from_quo(db, message, result, item)
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
    choice = _auto_choice(db, message, result, has_vehicle)
    if choice is None:
        return None
    template, text = choice
    key = _auto_key(message.conversationId, template)
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
    contact = item.contact_name or ""
    name = name or ("" if contact.casefold().startswith("new lead") else contact)
    vehicle = item.vehicle_text
    if not vehicle and item.vehicle_id:
        row = db.get(Vehicle, item.vehicle_id)
        vehicle = f"{row.year or ''} {row.make or ''} {row.model or ''}".strip() if row else None
    if vehicle and vehicle.casefold() in name.casefold():
        vehicle = None  # contact labels often already include the vehicle
    return " - ".join(part for part in (name, vehicle) if part) or item.customer_phone


def _next_step(db: Session, item: DispatchItem) -> str:
    """Drops the vehicle question once the record already shows the vehicle."""
    text = item.proposed_action or ""
    if item.vehicle_text or item.vehicle_id or re.search(rf"\b((19|20)\d\d|{MAKES})\b", _label(db, item), re.I):
        text = text.replace("Ask for: year, make and model, ", "Ask for: ").replace(" Ask for: year, make and model.", "")
    return text


def _snapshot_item(db: Session, item: DispatchItem) -> dict:
    return {"id": item.id, "who": _label(db, item), "phone": item.customer_phone, "type": item.category,
            "urgency": item.urgency, "lastMessage": item.summary[:200], "nextStep": _next_step(db, item)[:160],
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
    calendar = calendar_feed.upcoming(now)
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
        "calendarNext7Days": [calendar_feed.describe(e) for e in calendar[:30]] if calendar is not None else "Calendar is not connected or could not be read.",
        "calendarCoverage": "Only calendars linked to this assistant. Others can hold appointments this list cannot see.",
        "todaySoFar": _today_activity(db, start),
        "notInThisSnapshot": "Invoices and website forms.",
    }


def _active_item(db: Session, item_id: int):
    item = db.query(DispatchItem).filter(DispatchItem.id == item_id).with_for_update().first()
    return item if item is not None and item.status in FOLLOW_UP_STATUSES else None


def _mark_done(db: Session, item_ids: list[int]) -> str:
    closed, skipped = [], []
    for item_id in item_ids:
        item = _active_item(db, item_id)
        if item is None:
            skipped.append(item_id)
            continue
        item.status = "closed"
        item.decided_at = datetime.now().astimezone()
        audit(db, item.id, "owner_sms", "owner_done")
        closed.append(item_id)
    db.commit()
    parts = []
    if closed:
        parts.append("Closed " + ", ".join(f"#{n}" for n in closed) + ". Nothing was sent.")
    if skipped:
        parts.append(", ".join(f"#{n}" for n in skipped) + " not open.")
    return " ".join(parts)


def _snooze(db: Session, item_id: int, minutes: int) -> str:
    item = _active_item(db, item_id)
    if item is None:
        return f"#{item_id} is not open."
    due = datetime.now(PHOENIX) + timedelta(minutes=minutes)
    audit(db, item.id, "owner_sms", "follow_up_due", due.isoformat())
    db.commit()
    return f"OK. I'll remind you about #{item.id} around {due.strftime('%a %-I:%M %p')}."


def _remind_at(db: Session, item: DispatchItem) -> datetime | None:
    """The latest follow-up time set since the customer last wrote."""
    row = db.query(DispatchAudit).filter(DispatchAudit.item_id == item.id, DispatchAudit.action == "follow_up_due") \
        .order_by(DispatchAudit.id.desc()).first()
    if row is None or _utc(row.created_at) < _utc(item.updated_at) - timedelta(seconds=5):
        return None
    return datetime.fromisoformat(row.detail)


def _older_items(db: Session, now: datetime) -> list[DispatchItem]:
    rows = db.query(DispatchItem).filter(DispatchItem.status == "needs_owner").order_by(DispatchItem.id).all()
    return [i for i in rows if now - _utc(i.updated_at) >= STALE_AFTER]


def _waited(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m" if minutes < 1440 else f"{minutes // 1440}d"


def _follow_ups(db: Session, now: datetime) -> list[tuple[DispatchItem, str]]:
    """What the owner still owes a customer, urgent first, then longest waiting."""
    found = []
    rows = db.query(DispatchItem).filter(DispatchItem.status.in_(("needs_owner", "approved"))).all()
    for item in rows:
        age = now - _utc(item.updated_at)
        if age >= STALE_AFTER:
            continue
        remind = _remind_at(db, item)
        if remind is not None and remind > now:
            continue
        if item.status == "approved":
            if remind is not None:
                found.append((item, "promised"))
        elif remind is not None or age >= (URGENT_REMINDER if item.urgency != "normal" else NORMAL_REMINDER):
            found.append((item, "urgent" if item.urgency != "normal" and age < URGENT_WINDOW else "waiting"))
    return sorted(found, key=lambda row: (row[1] != "urgent", row[0].updated_at))


def _follow_up_line(db: Session, now: datetime, item: DispatchItem, kind: str) -> str:
    what = "You promised a reply." if kind == "promised" else _next_step(db, item)[:100]
    return (f"#{item.id} {_label(db, item)}, waiting {_waited(now - _utc(item.updated_at))}\n{what}\n"
            f"Tell me what to say, DONE {item.id}, or LATER {item.id}.")


def _owner_command(db: Session, message: IntakeMessage) -> dict:
    command = parse_owner_command(message.body)
    if command is None:
        return {"ownerQuestion": message.body.strip()[:1000], "snapshot": _owner_snapshot(db)}
    decision, item_id = command[:2]
    if decision == "pause":
        cancelled = pause(db, "owner_sms")
        note = f" {cancelled} waiting replies were cancelled." if cancelled else ""
        return {"ownerReply": f"Stopped. No customer texts or alerts will go out until you text RESUME.{note}"}
    if decision == "resume":
        resume(db, "owner_sms")
        return {"ownerReply": "Back on. Anything that came in while stopped will reach you now."}
    if decision == "done":
        return {"ownerReply": _mark_done(db, item_id)}
    if decision == "later":
        return {"ownerReply": _snooze(db, item_id, command[2])}
    if decision == "older":
        rows = _older_items(db, datetime.now(PHOENIX))
        if not rows:
            return {"ownerReply": "Nothing older is open."}
        return {"ownerReply": "\n".join(f"#{i.id} {_label(db, i)}" for i in rows[:10]) + "\nDONE with the numbers clears them."}
    if decision in ("approve", "edit") and is_paused(db):
        return {"ownerReply": "Texting is stopped, so nothing was sent. Text RESUME first."}
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
    if website_alerts.is_website(item) and decision in ("approve", "edit"):
        return {"ownerReply": f"Website request #{item.id} must be contacted manually."}
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
    if decision != "decline" and (not text or PROMISE.search(text)):
        audit(db, item.id, "system", "follow_up_due", (item.decided_at + PROMISE_AFTER).isoformat())
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
    if is_reaction(message.body):  # a thumbs-up is not a new request
        db.add(DispatchEvent(quo_message_id=message.messageId))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
        return {"recorded": False, "reaction": True}
    result = assess(message.body)
    for attempt in (1, 2):  # a concurrent delivery can win the unique index once
        try:
            item = _record(db, message, result)
            return {"recorded": True, "itemId": item.id, "status": item.status,
                    "vehicleText": item.vehicle_text, "category": item.category,
                    "autoReply": None if is_paused(db) else _auto_reply_for(db, item, message, result)}
        except IntegrityError:
            db.rollback()
            if db.query(DispatchEvent).filter(DispatchEvent.quo_message_id == message.messageId).first():
                return {"recorded": False, "duplicate": True}
            if attempt == 2:
                raise


FOLLOW_UP_STATUSES = ("routine", "needs_owner", "approved")
PROMISE_AFTER = timedelta(hours=2)
URGENT_REMINDER = timedelta(minutes=20)
NORMAL_REMINDER = timedelta(hours=2)
STALE_AFTER = timedelta(days=3)
URGENT_WINDOW = timedelta(hours=4)  # after this an item moves to the regular digests
NO_REPLY = "NO_REPLY_NEEDED"
DIGEST_HOURS = (13, 15, 17)
MAX_FOLLOW_UPS = 3  # per check, so a long list never floods the owner
DASHBOARD_REPLY = "dashboard_reply"
QUIET = timedelta(seconds=30)  # lets a burst of customer texts become one alert
TICK_LIMIT = 10
DAILY_HOUR = 10
STUCK_HOUR = 11
DRAFT_GRACE = timedelta(seconds=90)
APPOINTMENT_WARNING = timedelta(minutes=75)
FOLLOW_UP_HOURS = range(11, 19)


def _alert_text(db: Session, item: DispatchItem) -> str:
    said = " ".join(item.summary.split())[:120]
    name = _label(db, item)
    if item.category == approval_links.CATEGORY:
        return (f"#{item.id} Approval link for {name}. {said}\nSend: {(item.draft_reply or '')[:300]}\n"
                f"YES {item.id} texts it to them. NO {item.id} skips it.")
    if website_alerts.is_website(item) and item.category == "approval":
        return f"Estimate #{item.id} {name}: {said}\nOpen the job to start the approved work. DONE {item.id} when handled."
    if website_alerts.is_website(item):
        phone = item.customer_phone or "no phone"
        if item.draft_reply == "":
            return f"Website booking {name}: {said}\nNothing needed. It's on the calendar."
        head = "URGENT website" if item.urgency != "normal" else "Website"
        return (f"{head} #{item.id} {name} {phone}\n{said}\n"
                f"Call or text them yourself. Nothing was sent to them. DONE {item.id} when handled.")
    if item.draft_reply == "":
        return f"FYI {name}: {said}\nNothing needed. Tell me if you want to answer."
    head = f"URGENT #{item.id}" if item.urgency != "normal" else f"#{item.id}"
    lines = [f"{head} {name}", f"They said: {said}"]
    if item.draft_reply:
        lines.append(f"Send: {item.draft_reply[:240]}")
        lines.append(f"YES {item.id} sends it, or tell me what to say.")
    else:
        lines.append(f"Next: {_next_step(db, item)[:120]}")
        lines.append("Tell me what to say, or DONE " + str(item.id) + ".")
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
        f"App bookings today: {len(appointments)}" + (f" ({times})" if times else ""),
        f"Waiting on you: {len(waiting)}",
        f"Waiting on customers: {customers}",
        f"Other open messages: {routine}",
    ]
    calendar = calendar_feed.upcoming(now, days=1)
    if calendar is None:
        if os.getenv("CALENDAR_ICS_URL"):
            lines.insert(1, "Linked calendar: could not be read")
    else:
        lines.insert(1, "Linked calendar: " + ("; ".join(calendar_feed.describe(e, False) for e in calendar[:5]) or "nothing today"))
    lines += [f"#{i.id} {_label(db, i)}" for i in waiting[:4]]
    return "\n".join(lines)


def _backup_problem(db: Session, now: datetime) -> str | None:
    """Backups report themselves, so silence counts as failure too."""
    row = db.get(DispatchSync, BACKUP_SOURCE)
    if row is None:
        return None
    success = _utc(row.last_success_at)
    if (row.consecutive_failures or 0) >= 1 or success is None or now - success > BACKUP_STALE_AFTER:
        return "Nightly backup didn't finish. Your data is safe, but tell Claude so it can be checked."
    return None


def due_owner_messages(db: Session, now: datetime) -> list[dict]:
    """Everything owed to the owner right now. Failed texts come back next tick."""
    if not OWNER_PHONE:
        return []
    now = now.astimezone(PHOENIX)
    due: list[tuple[str, str, int | None, str]] = []
    for item in db.query(DispatchItem).filter(DispatchItem.status.in_(OPEN_STATUSES)).all():
        if item.status == "needs_owner" or item.urgency != "normal" or item.draft_reply == "":
            # Give the draft a moment to arrive, except on safety issues.
            waited = now - _utc(item.updated_at).astimezone(PHOENIX)
            if item.draft_reply is None and item.urgency != "safety" and waited < DRAFT_GRACE:
                continue
            if item.urgency == "normal" and timedelta(0) <= waited < QUIET:
                continue
            due.append((f"alert:{item.id}:{item.last_message_id}", "owner_alert", item.id, _alert_text(db, item)))
    if now.weekday() != 6 and now.hour in FOLLOW_UP_HOURS:
        queued = 0
        for item, kind in _follow_ups(db, now):
            if kind == "waiting" and now.hour not in DIGEST_HOURS:
                continue
            # Urgent repeats every half hour. Everything else comes once a day.
            key = f"urgent:{item.id}:{now.date()}:{now.hour}:{now.minute // 30}" if kind == "urgent" else f"nudges:{item.id}:{now.date()}"
            if not db.query(DispatchOutbound).filter(DispatchOutbound.dedupe_key == key).first():
                due.append((key, "owner_nudge", item.id, _follow_up_line(db, now, item, kind)))
                queued += 1
            if queued >= MAX_FOLLOW_UPS:
                break
    if now.weekday() != 6 and (now.hour, now.minute) >= (18, 30):
        rows = _follow_ups(db, now)
        promised = sum(1 for row in rows if row[1] == "promised")
        lines = [f"End of day: {len(rows) - promised} waiting on you, {promised} promised follow-ups"]
        older = len(_older_items(db, now))
        if older:
            lines.append(f"{older} older than 3 days. Text OLD to review.")
        due.append((f"wrap:{now.date()}", "owner_wrap", None, "\n".join(lines)))
    for event in calendar_feed.upcoming(now, days=1) or []:
        lead = event["start"] - now
        if not event["allDay"] and timedelta(0) < lead <= APPOINTMENT_WARNING:
            due.append((f"calendar:{event['start'].isoformat()}:{event['title'][:40]}", "owner_calendar", None,
                        f"Coming up: {calendar_feed.describe(event, False)}. Be at the shop before they arrive."))
    backup_problem = _backup_problem(db, now)
    if backup_problem and now.hour >= 7:
        due.append((f"backup:{now.date()}", "owner_backup", None, backup_problem))
    if now.weekday() != 6 and STUCK_HOUR <= now.hour < STUCK_HOUR + 3:
        # One text a day, only when something is actually stuck (exceptions, not a status report).
        stuck = next_action.stuck_jobs(db, now.astimezone(timezone.utc))
        if stuck:
            due.append((f"stuck:{now.date()}", "owner_stuck", None, next_action.stuck_text(stuck)))
    if os.getenv("SERVICE_LEADS_ENABLED", "").strip().lower() == "true":
        from app.lead_lifecycle import due_lead_digest
        lead_digest = due_lead_digest(db, now)
        if lead_digest:
            due.append((lead_digest[0], "owner_leads", None, lead_digest[1]))
    if now.weekday() != 6 and DAILY_HOUR <= now.hour < DAILY_HOUR + 3:
        due.append((f"daily:{now.date()}", "owner_daily", None, _daily_text(db, now)))

    current_rules = alert_rules.rules()
    kept = []
    for key, kind, item_id, text in due:
        item = db.get(DispatchItem, item_id) if item_id else None
        urgent = bool(item and item.urgency != "normal")
        booking = bool(item and website_alerts.is_website(item) and item.draft_reply == "")
        if alert_rules.allowed(kind, urgent, booking, now, current_rules):
            kept.append((key, kind, item_id, text))  # held ones are not reserved, so they go out later
    messages = []
    for key, kind, item_id, text in kept:
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


def queued_replies(db: Session) -> list[dict]:
    """Replies approved on the dashboard. Each is handed over once and never retried."""
    replies = []
    rows = db.query(DispatchOutbound).filter(DispatchOutbound.kind == DASHBOARD_REPLY, DispatchOutbound.status == "queued").all()
    for row in rows:
        item = db.get(DispatchItem, row.item_id)
        row.status = "reserved"
        if item is None or website_alerts.is_website(item) or not item.customer_phone or not item.draft_reply:
            row.status = "skipped"
            continue
        replies.append({"key": row.dedupe_key, "itemId": item.id, "phone": item.customer_phone,
                        "text": item.draft_reply, "messageId": item.thread_message_id or item.last_message_id})
    db.commit()
    return replies


def _name_sweep():
    """Background: a few unnamed customers get their name looked up in Quo. Never texts anyone."""
    if not os.environ.get("MESSAGING_BRIDGE_URL"):
        return
    try:
        from app.database import SessionLocal
        result = name_finder.sweep(SessionLocal)
        if result.get("found") or result.get("none") or result.get("unavailable"):
            print(f"name_sweep {result}")
    except Exception as err:  # a failed lookup must never break the alert tick
        print(f"name_sweep_failed {type(err).__name__}")


@internal_router.post("/tick")
def tick(background: BackgroundTasks, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    background.add_task(_name_sweep)
    if is_paused(db):  # nothing is reserved, so held alerts go out after RESUME
        return {"ownerPhone": OWNER_PHONE or None, "messages": [], "customerReplies": [], "paused": True}
    return {"ownerPhone": OWNER_PHONE or None, "messages": due_owner_messages(db, datetime.now(timezone.utc)),
            "customerReplies": queued_replies(db)}


@internal_router.post("/draft")
def save_draft(draft: DraftInput, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    """Keeps a draft only if it answers the conversation's latest message."""
    item = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == draft.conversationId,
        DispatchItem.status.in_(OPEN_STATUSES),
        DispatchItem.last_message_id == draft.messageId,
    ).first()
    if item is None or website_alerts.is_website(item):
        return {"recorded": False}
    reasons = json.loads(item.approval_reasons or "[]")
    if draft.note and draft.note.strip():
        item.proposed_action = draft.note.strip()  # the job file line for this customer
    if draft.body.strip() == NO_REPLY:
        if item.urgency != "normal" or reasons != ["customer_reply"]:
            db.commit()  # keep the job line; anything sensitive stays with the owner
            return {"recorded": False}
        item.draft_reply = ""  # an empty draft means no reply is needed
        item.status = "routine"
    else:
        item.draft_reply = draft.body.strip()
    audit(db, item.id, "system", "draft_saved")
    db.commit()
    return {"recorded": True, "itemId": item.id}


def _owner_part_cost(db: Session, request_: OwnerAction) -> dict:
    """Save private parts sourcing to a verified job, never a customer quote.

    No retail price, customer line item, outbound message, or approval link is
    created. The shop must verify fitment/labor and approve its selling price.
    """
    if not request_.ownerPhone or not _is_owner(request_.ownerPhone):
        raise HTTPException(status_code=403, detail="Owner identity required")
    message_id = (request_.ownerMessageId or "").strip()
    if not message_id or not db.query(DispatchEvent).filter(
        DispatchEvent.quo_message_id == message_id
    ).first():
        return {"ownerReply": "Could not verify the original owner text. Nothing changed."}
    item = _active_item(db, request_.itemId)
    if item is None:
        return {"ownerReply": "That customer item is no longer open. Please provide the repair-order number."}
    customer_id = item.customer_id
    if customer_id is None:
        phone = _normalize_phone(item.customer_phone)
        matches = [c for c in db.query(Customer).filter(Customer.phone.isnot(None)).all()
                   if _normalize_phone(c.phone) == phone]
        if len(matches) != 1:
            return {"ownerReply": "Cannot uniquely match the customer. Please provide the RO number; nothing changed."}
        customer_id = matches[0].id
    active = db.query(RepairOrder).filter(
        RepairOrder.customer_id == customer_id,
        RepairOrder.status != "delivered",
    ).with_for_update().all()
    if item.vehicle_id:
        active = [ro for ro in active if ro.vehicle_id == item.vehicle_id]

    evidence = (request_.evidence or "").strip()
    cost = request_.unitCost
    description = (request_.partDescription or "").strip()
    private_cost = re.search(
        r"\b(?:my|our|shop|vendor|supplier|wholesale|purchase)\s+"
        r"(?:(?:parts?|tank|item|unit)\s+)?cost\b|"
        r"\b(?:cost\s+(?:me|us)|(?:i|we)\s+paid)\b", evidence, re.I,
    )
    prices = re.findall(r"\$\s*[\d,]+(?:\.\d{1,2})?", evidence)
    quoted = any(
        abs(float(p.replace("$", "").replace(",", "").strip()) - cost) < 0.001
        for p in prices
    ) if cost is not None else False
    part_words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", description)
                  if w.lower() not in ("part", "parts", "replacement", "assembly")]
    mentioned = any(w in evidence.lower() for w in part_words)
    if not private_cost or not quoted or not description or not mentioned:
        return {"ownerReply": "Could not verify that part and private cost from your text. Nothing changed."}

    # The original cost message must name the same vehicle as the selected RO.
    # A label or model-selected item number alone is not sufficient.
    evidence_l = evidence.lower()
    verified = []
    for ro in active:
        vehicle = db.get(Vehicle, ro.vehicle_id)
        if vehicle and vehicle.make and vehicle.model:
            if vehicle.make.lower() in evidence_l and vehicle.model.lower() in evidence_l:
                verified.append(ro)
    if len(verified) != 1:
        return {"ownerReply": "I need the exact RO number to safely match that part. Nothing changed."}
    ro = verified[0]

    audit_key = f"owner-msg:{message_id}"
    already = db.query(DispatchAudit).filter(
        DispatchAudit.item_id == item.id,
        DispatchAudit.action == "part_cost_staged",
        DispatchAudit.detail == audit_key,
    ).first()
    if already:
        return {"ownerReply": f"Already recorded for RO #{ro.id}. No duplicate part was created."}

    eta = (request_.eta or "").strip()
    if eta and not any(w.lower() in evidence_l for w in re.findall(r"[A-Za-z]{4,}", eta)):
        eta = ""
    desc = description + (f" — anticipated pickup {eta} (unconfirmed)" if eta else "")
    sources = db.query(PartsOrder).filter(
        PartsOrder.repair_order_id == ro.id,
        PartsOrder.status == "sourcing",
    ).with_for_update().all()
    candidates = [p for p in sources if
                  (p.description or "").split(" — anticipated pickup")[0].lower() == description.lower()]
    if len(candidates) > 1:
        return {"ownerReply": "Multiple sourcing records matched. Review the RO; nothing changed."}
    part = candidates[0] if candidates else PartsOrder(
        repair_order_id=ro.id, quantity=1, vendor=None, status="sourcing")
    part.description = desc
    part.cost = cost
    db.add(part)
    db.flush()

    # Keep one pricing task linked to the existing customer and RO.
    queue_key = f"PARTPRICE{ro.id}"
    task = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == queue_key,
        DispatchItem.status.in_(OPEN_STATUSES),
    ).with_for_update().first()
    now = datetime.now(timezone.utc)
    if task is None:
        task = DispatchItem(quo_conversation_id=queue_key, customer_phone=item.customer_phone,
                            customer_id=ro.customer_id, vehicle_id=ro.vehicle_id,
                            category="estimate_pricing", urgency="normal",
                            status="needs_owner", last_message_id=message_id,
                            created_at=now, updated_at=now)
        db.add(task)
    task.summary = f"RO #{ro.id}: {description} sourced internally. Customer quote needs owner review."
    task.proposed_action = "Verify fitment, selling price, labor and taxes before sending an estimate."
    task.draft_reply = None
    task.updated_at = now
    db.add(DispatchAudit(item_id=item.id, actor="owner_sms",
                         action="part_cost_staged", detail=audit_key))
    db.commit()
    return {"ownerReply": (
        f"Saved {description} to RO #{ro.id} as private sourcing cost USD {cost:.2f}"
        + (f" (pickup {eta}, unconfirmed)" if eta else "")
        + ". No customer quote or message sent. Next: approve selling price and labor."
    )}


def _owner_quote_part(db: Session, request_: OwnerAction) -> dict:
    """Build pending quote lines ONLY from explicit owner price + labor command.

    Never sends a customer SMS, charges an invoice or queues a public link.
    Pending items are non-billable until the customer approves them.
    """
    if not request_.ownerPhone or not _is_owner(request_.ownerPhone):
        raise HTTPException(status_code=403, detail="Owner identity required")
    message_id = (request_.ownerMessageId or "").strip()
    if not message_id or db.query(DispatchEvent).filter(
        DispatchEvent.quo_message_id == message_id).first() is None:
        return {"ownerReply": "Owner message not verified. No quote was changed."}
    ro_id, part_price, hours, kind = (
        request_.roId, request_.partPrice, request_.laborHours, request_.rateType)
    evidence = (request_.evidence or "").strip()
    if not ro_id or part_price is None or hours is None or kind is None:
        return {"ownerReply": "Please specify RO #, customer part price, labor hours and rate type."}
    if re.search(r"\b(?:my|our|vendor|supplier|wholesale)\s+cost\b", evidence, re.I):
        return {"ownerReply": "That is a private cost, not a customer quote. Nothing changed."}
    if not re.search(rf"\b(?:RO|job)\s*#?\s*{ro_id}\b", evidence, re.I):
        return {"ownerReply": "Confirm the RO number in your price instruction. Nothing changed."}
    part_match = re.search(
        r"\b(?:customer\s+(?:part\s+)?price|selling\s+price|retail\s+price)"
        r"\s*(?:is|of|:|=|at)?\s*\$\s*([\d,]+(?:\.\d{1,2})?)", evidence, re.I)
    hours_match = re.search(
        r"\blabor\s*(?:hours?|time|:|=)?\s*(\d+(?:\.\d{1,2})?)\s*(?:hours?|hrs?|h)\b",
        evidence, re.I)
    if (not part_match or not hours_match
            or abs(float(part_match.group(1).replace(",", "")) - part_price) > 0.001
            or abs(float(hours_match.group(1)) - hours) > 0.001
            or not re.search(rf"\b{kind}\b", evidence, re.I)):
        return {"ownerReply": "Price, labor and rate were not verifiable from your text. Nothing changed."}

    ro = db.query(RepairOrder).filter(
        RepairOrder.id == ro_id,
        RepairOrder.status != "delivered",
    ).with_for_update().first()
    if ro is None:
        return {"ownerReply": "That repair order is missing or already delivered. Nothing changed."}
    sourced = db.query(PartsOrder).filter(
        PartsOrder.repair_order_id == ro.id,
        PartsOrder.status == "sourcing",
    ).with_for_update().all()
    if len(sourced) != 1:
        return {"ownerReply": "This RO needs exactly one verified sourcing part for this shortcut. Review the RO manually."}
    part = sourced[0]
    if part_price < part.cost:
        return {"ownerReply": "The customer price is below the recorded shop cost. Review this job manually."}
    audit_key = f"quote-ro:{ro.id}-owner-msg:{message_id}"
    duplicate = db.query(DispatchAudit).filter(
        DispatchAudit.action == "part_quote_staged",
        DispatchAudit.detail == audit_key,
    ).first()
    if duplicate:
        return {"ownerReply": f"Draft quote already saved to RO #{ro.id}; nothing duplicated."}

    clean_part = (part.description or "Part").split(" — anticipated pickup")[0].strip()
    descriptions = (f"Part: {clean_part}", f"Labor: install {clean_part}")
    existing = db.query(LineItem).filter(
        LineItem.repair_order_id == ro.id,
        LineItem.description.in_(descriptions),
    ).with_for_update().all()
    if len(existing) > 2 or any(li.approval in ("approved", "declined") for li in existing):
        return {"ownerReply": "Existing customer-approved or declined lines need manual review. Nothing changed."}
    by_description = {li.description: li for li in existing}
    for desc, typ, quantity, price, taxable in (
        (descriptions[0], "part", 1, part_price, True),
        (descriptions[1], "labor", hours, pricing.rate_for(kind), False),
    ):
        line = by_description.get(desc)
        if line is None:
            line = LineItem(repair_order_id=ro.id, description=desc, approval="pending")
            db.add(line)
        line.item_type = typ
        line.quantity = quantity
        line.unit_price = price
        line.taxable = taxable
        line.approval = "pending"
    db.add(DispatchAudit(actor="owner_sms", action="part_quote_staged", detail=audit_key))
    db.commit()
    subtotal = part_price + hours * pricing.rate_for(kind)
    return {"ownerReply": (
        f"RO #{ro.id}: saved PENDING part USD {part_price:.2f} plus {hours:g} "
        f"hours {kind} labor at USD {pricing.rate_for(kind):.2f}/hr "
        f"(subtotal USD {subtotal:.2f}, tax extra if applicable). "
        "Nothing sent or billed. Verify fitment and request approval from the RO."
    )}


def _owner_prepare_link(db: Session, request_: OwnerAction) -> dict:
    """Queue existing customer approval link; Quo's latest-thread guard sends.

    An explicit owner command attests fitment. Even then the link is only
    queued for owner YES. Nothing here reaches the customer automatically.
    """
    if not request_.ownerPhone or not _is_owner(request_.ownerPhone):
        raise HTTPException(status_code=403, detail="Owner identity required")
    message_id = (request_.ownerMessageId or "").strip()
    evidence = (request_.evidence or "").strip()
    ro_id = request_.roId
    if (not ro_id or not message_id or db.query(DispatchEvent).filter(
        DispatchEvent.quo_message_id == message_id).first() is None
        or not re.fullmatch(
            rf"prepare\s+(?:customer\s+)?(?:estimate\s+)?approval\s+"
            rf"(?:link\s+)?(?:for\s+)?(?:RO|job)\s*#?\s*{ro_id}"
            rf"\s*[,;:.]?\s*fitment\s+verified[.!]?", evidence, re.I
        )):
        return {"ownerReply": "Explicit RO and fitment-verification command required. Nothing changed."}
    ro = db.query(RepairOrder).filter(
        RepairOrder.id == ro_id, RepairOrder.status != "delivered"
    ).with_for_update().first()
    if ro is None:
        return {"ownerReply": "Repair order not found or already delivered; nothing changed."}
    lines = db.query(LineItem).filter(LineItem.repair_order_id == ro.id).all()
    pending = [li for li in lines if li.approval == "pending"]
    if (not pending or any(li.approval is None for li in lines)
            or any(li.unit_price is None or li.unit_price < 0 or li.quantity is None
                   or li.quantity < 0 for li in pending)):
        return {"ownerReply": "The RO has incomplete or unreviewed prices. Nothing queued."}
    phone = approval_links.mobile(ro.customer.phone)
    if not phone:
        return {"ownerReply": "No valid customer phone on the RO. Nothing queued."}
    existing = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == approval_links.conversation_key(ro.id),
        DispatchItem.status.in_(("routine", "needs_owner", "approved")),
    ).first()
    if existing:
        return {"ownerReply": f"RO #{ro.id} already has approval item #{existing.id}. Review it before sending another link."}
    if not request_.customerMessageId:
        return {"customerThreadPhone": phone,
                "ownerReply": "Waiting to check current customer Quo messages before queuing the link."}
    try:
        task = approval_links.queue(db, ro, request_.customerMessageId, "owner_sms")
    except ValueError:
        return {"ownerReply": "Couldn't prepare the approval link safely; review the RO in the dashboard."}
    ro.status = "waiting_approval"
    # The pricing task is superseded by one actionable customer-approval item.
    old = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == f"PARTPRICE{ro.id}",
        DispatchItem.status.in_(OPEN_STATUSES),
    ).all()
    for item in old:
        item.status = "closed"
        item.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"ownerReply": (
        f"RO #{ro.id}: approval link drafted as item #{task.id}. Nothing sent. "
        f"Review it, then approve the customer message through the existing YES #{task.id} flow "
        "after its alert, or use the dashboard."
    )}


@internal_router.post("/owner-action")
def owner_action(request_: OwnerAction, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    """Plain-English owner replies. Nothing here texts a customer: sending still needs YES."""
    if request_.action == "prepare_link":
        return _owner_prepare_link(db, request_)
    if request_.action == "quote_part":
        return _owner_quote_part(db, request_)
    if request_.action == "part_cost":
        return _owner_part_cost(db, request_)
    if request_.action == "done":
        return {"ownerReply": _mark_done(db, [request_.itemId])}
    if request_.action == "later":
        return {"ownerReply": _snooze(db, request_.itemId, (request_.hours or 2) * 60)}
    item = _active_item(db, request_.itemId)
    text = (request_.text or "").strip()
    if item is None or not text:
        return {"ownerReply": "I couldn't match that to an open customer. Text OPEN for the list."}
    if website_alerts.is_website(item):
        return {"ownerReply": f"Website request #{item.id} must be contacted manually."}
    item.draft_reply = text
    item.status = "needs_owner"
    audit(db, item.id, "owner_sms", "owner_proposed")
    db.commit()
    return {"ownerReply": f"Send to {_label(db, item)}:\n{text}\nReply YES {item.id} to send."}


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


class HandledInput(BaseModel):
    conversationId: str = Field(min_length=1, max_length=64)
    at: datetime


@internal_router.post("/handled")
def handled(report: HandledInput, db: Session = Depends(get_db), _auth: bool = Depends(require_internal_key)):
    """Closes an open item once someone has answered the customer in Quo."""
    item = db.query(DispatchItem).filter(
        DispatchItem.quo_conversation_id == report.conversationId,
        DispatchItem.status.in_(FOLLOW_UP_STATUSES),
    ).first()
    if item is None or _is_owner(item.customer_phone) or _utc(report.at) <= _utc(item.updated_at):
        return {"closed": False}
    if item.status == "approved" and _utc(report.at) <= _utc(item.decided_at) + timedelta(minutes=2):
        return {"closed": False}  # that text is the approved reply itself
    item.status = "closed"
    item.decided_at = item.updated_at = datetime.now().astimezone()
    audit(db, item.id, "system", "replied_in_quo")
    db.commit()
    return {"closed": True}


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


@router.post("/sending")
def set_sending(request: Request, action: str = Form(...), db: Session = Depends(get_db)):
    """The dashboard's stop switch. It works in read-only mode because it only stops things."""
    user = get_current_user(request, db)
    if action == "stop":
        pause(db, user.username)
    elif action == "resume":
        resume(db, user.username)
    else:
        raise HTTPException(422, "Choose stop or resume.")
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/{item_id}/decide")
def decide(
    item_id: int,
    request: Request,
    decision: str = Form(...),
    expected_message_id: str = Form(...),
    proposed_action: str = Form(default=""),
    reply_text: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if decision not in ("approve", "decline"):
        raise HTTPException(422, "Decision must be approve or decline.")
    if decision == "approve" and reply_text.strip() and is_paused(db):
        raise HTTPException(409, "Texting is stopped. Resume it before sending.")
    item = db.query(DispatchItem).filter(DispatchItem.id == item_id).with_for_update().first()
    if item is None:
        raise HTTPException(404, "Item not found.")
    if item.status not in OPEN_STATUSES:
        raise HTTPException(409, "This item was already decided.")
    if website_alerts.is_website(item) and decision == "approve" and reply_text.strip():
        raise HTTPException(409, "Website requests must be contacted manually.")
    if item.last_message_id != expected_message_id:
        raise HTTPException(409, "A newer customer message arrived. Review it before deciding.")
    if proposed_action.strip():
        item.proposed_action = proposed_action.strip()[:2000]
    item.status = "approved" if decision == "approve" else "declined"
    item.decided_by = user.id
    item.decided_at = datetime.now().astimezone()
    item.updated_at = item.decided_at
    audit(db, item.id, user.username, f"owner_{decision}")
    text = reply_text.strip()[:1500]
    if decision == "approve" and text:
        item.draft_reply = text  # the messaging service sends it on its next tick
        db.add(DispatchOutbound(dedupe_key=f"dashboard:{item.id}:{item.last_message_id}", kind=DASHBOARD_REPLY, item_id=item.id, status="queued"))
        if PROMISE.search(text):
            audit(db, item.id, "system", "follow_up_due", (item.decided_at + PROMISE_AFTER).isoformat())
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
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
BACKUP_SOURCE = "backup"
BACKUP_STALE_AFTER = timedelta(hours=30)
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
        "start_job": _start_job_url(customer, item),
    }


def _start_job_url(customer, item) -> str:
    """One tap from a text or call to a job, carrying over what the customer already told us."""
    from urllib.parse import urlencode
    concern = " ".join((item.summary or "").split())[:300]
    if customer:
        return "/ro/new?" + urlencode({"customer_id": customer.id, "concern": concern})
    words = (item.vehicle_text or "").split()
    year = words.pop(0) if words and words[0].isdigit() and len(words[0]) == 4 else ""
    name = (item.contact_name or "").split()
    query = {"phone": item.customer_phone, "first": name[0] if name else "", "last": " ".join(name[1:]),
             "year": year, "make": words[0] if words else "", "model": " ".join(words[1:]), "concern": concern}
    return "/customers/new?" + urlencode({k: v for k, v in query.items() if v})


def _sync_health(db: Session) -> list[dict]:
    now = datetime.now(timezone.utc)
    health = []
    for row in db.query(DispatchSync).order_by(DispatchSync.source).all():
        success = _utc(row.last_success_at)
        nightly = row.source == BACKUP_SOURCE
        failing = (row.consecutive_failures or 0) >= (1 if nightly else ESCALATE_AFTER_FAILURES)
        stale = success is None or now - success > (BACKUP_STALE_AFTER if nightly else SYNC_STALE_AFTER)
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
        paused = is_paused(db)
    except SQLAlchemyError:
        db.rollback()
        return {"dispatch_ready": False, "dispatch_items": [], "dispatch_policies": [],
                "dispatch_view": view, "dispatch_views": VIEWS, "dispatch_health": [], "dispatch_auto_policies": []}
    calendar = calendar_feed.upcoming(datetime.now(PHOENIX))
    return {
        "calendar_events": [calendar_feed.describe(e) for e in calendar[:10]] if calendar is not None else None,
        "dispatch_ready": True,
        "sending_paused": paused,
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
