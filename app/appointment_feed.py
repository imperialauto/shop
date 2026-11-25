"""Owner-only calendar subscription of dashboard appointments (iCal / ICS).

The owner turns it on in Settings and subscribes once on his iPhone; Apple Calendar
then refreshes it on its own. The address carries a long random code. Only a SHA-256
hash of the code is kept (business setting "appointment_feed"), so the full address is
shown once, when it is made. "New address" replaces it and the old one stops working at
once; "Turn off" removes it.

What an event shows, on purpose: the customer's first name, the vehicle, the job
number and its labor lines, the bay (or mobile), and whether it is confirmed. Never a
phone number, address, email, price or the customer's notes. Booked times only;
cancelled visits drop off. Open dashboard tasks with a due time show too, as reminders
(the appointment's own reminder task is left out: the visit is already there).

Off unless APPOINTMENT_FEED_ENABLED=true. Reads only; writes nothing to any calendar.
"""
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app import business_settings
from app.appointment_followup import SOURCE as APPOINTMENT_TASK_SOURCE, first_name
from app.appointment_model import Appointment
from app.imperial_task_model import ImperialTask
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder

SETTING = "appointment_feed"
PATH = "/calendar-feed/"
PAST_DAYS = 30
FUTURE_DAYS = 365
REMIND_MINUTES = 60
SHOWN = ("tentative", "confirmed", "completed")
TASKS_OPEN = ("open", "in_progress", "blocked")
TASK_REMIND_MINUTES = 15
TASK_MINUTES = 15
JOB_CHARS = 80
STATUS_WORDS = {"tentative": "Not confirmed", "confirmed": "Confirmed", "completed": "Done"}
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")

log = logging.getLogger("uvicorn.error")
public_router = APIRouter()
owner_router = APIRouter()


def enabled() -> bool:
    return os.getenv("APPOINTMENT_FEED_ENABLED", "").strip().lower() == "true"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _stored() -> dict:
    try:
        data = json.loads(business_settings.raw(SETTING) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def status() -> dict:
    data = _stored()
    return {"on": bool(data.get("hash")), "created_at": data.get("created_at")}


def issue(db: Session, actor: str) -> str:
    """A new address. The old one, if any, stops working. Caller commits."""
    token = secrets.token_urlsafe(32)
    business_settings.save(db, SETTING, {"hash": _hash(token),
                                         "created_at": datetime.now(timezone.utc).isoformat()}, actor)
    business_settings.clear_cache()
    return token


def turn_off(db: Session, actor: str) -> None:
    business_settings.save(db, SETTING, {}, actor)
    business_settings.clear_cache()


def valid(token: str) -> bool:
    expected = _stored().get("hash")
    return bool(expected and token) and secrets.compare_digest(_hash(token), expected)


# ---------------------------------------------------------------- ICS text

def _escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545: lines longer than 75 octets continue on the next line after a space."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for char in line:
        piece = char.encode()
        if len(current) + len(piece) > (75 if not parts else 74):
            parts.append(current.decode())
            current = b""
        current += piece
    parts.append(current.decode())
    return "\r\n ".join(parts)


def _stamp(value: datetime) -> str:
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def bay_label(appointment) -> str:
    if appointment.location == "mobile":
        return "Mobile"
    raw = (appointment.resource_id or "").strip()
    match = re.fullmatch(r"bay[-_ ]?(\w+)", raw, re.IGNORECASE)
    return f"Bay {match.group(1)}" if match else (raw.replace("-", " ").title() or "Shop")


def _private_free(text: str) -> str:
    """Phone numbers never go on the calendar, even when someone typed one into a title."""
    return " ".join(_PHONE.sub("", text or "").split())


def _job(order) -> str:
    """What the work is, from the job's labor lines (never the customer's own notes, which can
    hold an address). Declined lines are left out."""
    if not order:
        return ""
    names = [_private_free(" ".join((line.description or "").split()))
             for line in sorted(order.line_items or [], key=lambda line: line.id or 0)
             if (line.item_type or "labor") == "labor" and line.approval != "declined"]
    names = [name for name in names if name]
    text = "; ".join(names[:2]) + (f" (+{len(names) - 2} more)" if len(names) > 2 else "")
    if len(text) > JOB_CHARS:
        text = text[:JOB_CHARS - 1].rstrip() + "\u2026"
    return text


def _alarm(text: str, minutes: int) -> list[str]:
    return ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(text)}",
            f"TRIGGER:-PT{minutes}M", "END:VALARM"]


def event_lines(appointment, order, base: str, now: datetime) -> list[str]:
    who = first_name(order.customer) if order else "Customer"
    vehicle = order.vehicle.label if order and order.vehicle else "Vehicle details not given yet"
    tentative = appointment.status == "tentative"
    mobile = appointment.location == "mobile"
    bay = bay_label(appointment)
    summary = f"{'Not confirmed: ' if tentative else ''}{who}, {vehicle} ({bay})"
    job = _job(order)
    status_word = STATUS_WORDS.get(appointment.status, appointment.status.title())
    description = "\n".join(part for part in (
        f"Job #{appointment.repair_order_id}" + (f": {job}" if job else ""),
        f"{bay}. {'Mobile visit' if mobile else 'At the shop'}.",
        f"Status: {status_word}.",
        f"Open the job: {base}/ro/{appointment.repair_order_id}",
    ) if part)
    lines = [
        "BEGIN:VEVENT",
        f"UID:appointment-{appointment.id}@imperial-operations",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(appointment.starts_at)}",
        f"DTEND:{_stamp(appointment.ends_at)}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        f"LOCATION:{_escape('Mobile visit' if mobile else f'Imperial Auto Care, {bay}')}",
        f"STATUS:{'TENTATIVE' if tentative else 'CONFIRMED'}",
        f"URL:{base}/ro/{appointment.repair_order_id}",
    ]
    if appointment.status != "completed":
        lines += _alarm(summary, REMIND_MINUTES)
    lines.append("END:VEVENT")
    return lines


def task_lines(task, base: str, now: datetime) -> list[str]:
    title = _private_free(task.title) or "Shop task"
    summary = f"Reminder: {title}"
    state = "Waiting on someone" if task.status == "blocked" else "Open"
    description = f"{state}. Open the task: {base}/dashboard/tasks?task={task.id}"
    return [
        "BEGIN:VEVENT",
        f"UID:task-{task.id}@imperial-operations",
        f"DTSTAMP:{_stamp(now)}",
        f"DTSTART:{_stamp(task.due_at)}",
        f"DTEND:{_stamp(_aware(task.due_at) + timedelta(minutes=TASK_MINUTES))}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
        *_alarm(summary, TASK_REMIND_MINUTES),
        "END:VEVENT",
    ]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _tasks(db: Session, now: datetime) -> list:
    return db.query(ImperialTask).filter(
        ImperialTask.status.in_(TASKS_OPEN),
        ImperialTask.due_at.isnot(None),
        ImperialTask.source != APPOINTMENT_TASK_SOURCE,
        ImperialTask.due_at >= now - timedelta(days=PAST_DAYS),
        ImperialTask.due_at <= now + timedelta(days=FUTURE_DAYS),
    ).order_by(ImperialTask.due_at).limit(500).all()


def build(db: Session, base: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    rows = db.query(Appointment).filter(
        Appointment.status.in_(SHOWN),
        Appointment.starts_at >= now - timedelta(days=PAST_DAYS),
        Appointment.starts_at <= now + timedelta(days=FUTURE_DAYS),
    ).order_by(Appointment.starts_at).limit(1000).all()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Imperial Auto Care//Imperial Operations//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Imperial appointments",
             "X-WR-TIMEZONE:America/Phoenix", "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
             "X-PUBLISHED-TTL:PT15M"]
    for appointment in rows:
        lines += event_lines(appointment, db.get(RepairOrder, appointment.repair_order_id), base, now)
    for task in _tasks(db, now):
        lines += task_lines(task, base, now)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


def feed_url(request: Request, token: str) -> str:
    return f"{_base(request)}{PATH}{token}.ics"


# ---------------------------------------------------------------- routes

@public_router.get(PATH + "{name}")
def feed(name: str, request: Request, db: Session = Depends(get_db)):
    token = name[:-4] if name.endswith(".ics") else name
    if not enabled() or not valid(token):
        raise HTTPException(404, "Not found")
    return Response(build(db, _base(request)), media_type="text/calendar; charset=utf-8",
                    headers={"Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow",
                             "Referrer-Policy": "no-referrer"})


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can manage the calendar feed.")
    if not enabled():
        raise HTTPException(404, "Not found")
    return user


@owner_router.post("/settings/users/calendar-feed/off")
def turn_off_feed(request: Request, db: Session = Depends(get_db)):
    owner = _owner(request, db)
    turn_off(db, owner.username)
    db.commit()
    log.info("appointment_feed_off by_user=%s", owner.id)
    return RedirectResponse("/settings#calendar-feed", status_code=303)
