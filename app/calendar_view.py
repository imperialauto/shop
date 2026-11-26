"""What the Calendar tab shows: who is coming, in what, for what, and free times for a job.

Read-only. Booking and status changes still go through app.routes.appointments, so the
rules (shop hours, buffers, conflicts, Postgres lock) live in one place.
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session, joinedload, selectinload

from app.appointment_model import Appointment, BookingSlot
from app.appointments import PHOENIX, AppointmentWindow, overlaps
from app.models import RepairOrder

ACTIVE = ("tentative", "confirmed")
STEP_MINUTES = 30
DAYS_AHEAD = 14
LENGTHS = (1, 2, 3, 4, 6)
TRAVEL_CHOICES = (15, 30, 45, 60, 90, 120)
DEFAULT_BAY = "bay 1"  # what the old form saved ("Bay 1", lower-cased by the booking API)
WHAT_CHARS = 120

STATUS_TEXT = {
    "tentative": "Tentative",
    "confirmed": "Confirmed",
    "completed": "Done",
    "cancelled": "Cancelled",
}


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def local(value: datetime) -> datetime:
    return aware(value).astimezone(PHOENIX)


def clock(value: datetime) -> str:
    return local(value).strftime("%-I:%M %p")


def day_label(value: date, today: date) -> str:
    if value == today:
        return "Today, " + value.strftime("%a %b %-d")
    if value == today + timedelta(days=1):
        return "Tomorrow, " + value.strftime("%a %b %-d")
    return value.strftime("%A, %b %-d")


def customer_name(customer) -> str:
    if customer is None:
        return "Customer not named yet"
    first = (customer.first_name or "").strip()
    last = (customer.last_name or "").strip()
    if not first or first.lower().startswith("unknown"):
        return "Customer not named yet"
    return f"{first} {last}".strip()


def phone_text(raw: str | None) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return (raw or "").strip()


def phone_link(raw: str | None) -> str:
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return f"tel:+{digits}" if len(digits) == 11 else ""


def bay_name(resource_id: str, location: str = "shop") -> str:
    from app.appointment_feed import bay_label
    return bay_label(type("Slot", (), {"resource_id": resource_id, "location": location})())


def what_for(order) -> str:
    """Short line for the slot: the job's labor lines, else the start of the concern."""
    if order is None:
        return ""
    from app.appointment_feed import _job
    text = _job(order)
    if text:
        return text
    concern = " ".join((order.concern or "").split())
    if len(concern) > WHAT_CHARS:
        concern = concern[:WHAT_CHARS - 1].rstrip() + "…"
    return concern


def _job_status(order) -> str:
    from app.routes.repair_orders import RO_STATUSES
    return dict(RO_STATUSES).get(order.status or "", (order.status or "").replace("_", " ").title()) if order else ""


def actions_for(item: Appointment, now: datetime, may_change: bool, may_mark_missed: bool) -> list[tuple[str, str]]:
    """Same transitions the API allows: (action, button text)."""
    if not may_change:
        return []
    if item.status == "tentative":
        actions = [("confirm", "Confirm"), ("cancel", "Cancel")]
    elif item.status == "confirmed":
        actions = [("complete", "Mark done"), ("cancel", "Cancel")]
        if may_mark_missed and aware(item.ends_at) <= now:
            actions.append(("missed", "Mark missed"))
    else:
        actions = []
    return actions


def row(item: Appointment, order, missed: bool, now: datetime, may_change: bool, may_mark_missed: bool) -> dict:
    customer = order.customer if order else None
    vehicle = order.vehicle if order else None
    status = "missed" if missed else item.status
    return {
        "id": item.id,
        "status": status,
        "status_text": "Missed" if missed else STATUS_TEXT.get(item.status, item.status.title()),
        "day": local(item.starts_at).date(),
        "time": f"{clock(item.starts_at)} – {clock(item.ends_at)}",
        "starts_iso": aware(item.starts_at).isoformat(),
        "customer": customer_name(customer),
        "phone": phone_text(customer.phone) if customer else "",
        "phone_link": phone_link(customer.phone) if customer else "",
        "vehicle": vehicle.label if vehicle else "Vehicle details not given yet",
        "what": what_for(order) or "No concern written on the job yet",
        "concern": (order.concern or "").strip() if order else "",
        "notes": (order.tech_notes or "").strip() if order else "",
        "job_id": item.repair_order_id,
        "job_status": _job_status(order),
        "bay": bay_name(item.resource_id, item.location),
        "mobile": item.location == "mobile",
        "travel": item.travel_before_minutes or 0,
        "booked_value": float(item.booked_value or 0),
        "past": aware(item.ends_at) <= now,
        "actions": actions_for(item, now, may_change, may_mark_missed),
    }


def jobs_for(db: Session, items) -> dict:
    """The jobs behind these appointments, with customer, vehicle and lines loaded in one go."""
    ids = {item.repair_order_id for item in items}
    if not ids:
        return {}
    orders = db.query(RepairOrder).filter(RepairOrder.id.in_(ids)).options(
        joinedload(RepairOrder.customer), joinedload(RepairOrder.vehicle),
        selectinload(RepairOrder.line_items)).all()
    return {order.id: order for order in orders}


def missed_ids(db: Session) -> set[str]:
    from app.dispatch_model import DispatchAudit
    return {detail for (detail,) in db.query(DispatchAudit.detail).filter_by(action="followup_missed_visit")}


def calendar_sections(db: Session, now: datetime, may_change: bool, may_mark_missed: bool) -> dict:
    """Coming up (by day), past visits still open, and the last two weeks of closed ones."""
    today = now.astimezone(PHOENIX).date()
    since = now - timedelta(days=14)
    items = db.query(Appointment).filter(Appointment.ends_at >= since.astimezone(timezone.utc) - timedelta(days=1)) \
        .order_by(Appointment.starts_at).limit(400).all()
    jobs = jobs_for(db, items)
    missed = missed_ids(db)
    days: dict[date, list] = {}
    open_past, closed = [], []
    for item in items:
        if aware(item.ends_at) < since:
            continue
        entry = row(item, jobs.get(item.repair_order_id), str(item.id) in missed, now, may_change, may_mark_missed)
        if item.status in ACTIVE and aware(item.ends_at) > now:
            days.setdefault(entry["day"], []).append(entry)
        elif item.status in ACTIVE:
            open_past.append(entry)
        else:
            closed.append(entry)
    upcoming = [{"label": day_label(d, today), "rows": rows} for d, rows in sorted(days.items())]
    closed.reverse()
    return {"upcoming": upcoming, "open_past": open_past, "closed": closed,
            "upcoming_count": sum(len(d["rows"]) for d in upcoming),
            "tentative_count": sum(1 for d in upcoming for r in d["rows"] if r["status"] == "tentative")}


# ── Picking a time for one job ────────────────────────────────────────────────

def known_bays(db: Session) -> list[str]:
    ids = {r for (r,) in db.query(Appointment.resource_id).filter(Appointment.status.in_(ACTIVE)).distinct()}
    ids |= {r for (r,) in db.query(BookingSlot.resource_id).filter(BookingSlot.removed_at.is_(None)).distinct()}
    ids = {i.strip().casefold() for i in ids if i and i.strip()}
    return sorted(ids or {DEFAULT_BAY})


def free_starts(day: date, hours: dict, busy: list[AppointmentWindow], resource: str, minutes: int,
                travel: int, buffer: int, now: datetime) -> list[datetime]:
    """Start times on one day where the job fits in shop hours and clears every booking on that bay."""
    window = hours.get(day.weekday())
    if window is None:
        return []
    opens, closes = window
    start = datetime.combine(day, opens, PHOENIX)
    last = datetime.combine(day, closes, PHOENIX) - timedelta(minutes=minutes)
    found = []
    while start <= last:
        candidate = AppointmentWindow(start, start + timedelta(minutes=minutes), resource, travel, buffer)
        if start > now and not any(overlaps(candidate, other) for other in busy):
            found.append(start)
        start += timedelta(minutes=STEP_MINUTES)
    return found


def schedule_view(db: Session, order: RepairOrder, now: datetime, hours: dict, buffer: int, *,
                  day: str | None, length: int, bay: str | None, where: str, travel: int) -> dict:
    length = length if length in LENGTHS else 1
    where = "mobile" if where == "mobile" else "shop"
    travel = travel if travel in TRAVEL_CHOICES else 30
    bays = known_bays(db)
    bay = (bay or "").strip().casefold() or bays[0]
    if bay not in bays:
        bays.append(bay)
    travel_used = travel if where == "mobile" else 0
    buffer_used = buffer if where == "shop" else 0
    today = now.astimezone(PHOENIX).date()
    horizon_end = datetime.combine(today + timedelta(days=DAYS_AHEAD + 1), time(), PHOENIX)
    busy_rows = db.query(Appointment).filter(
        Appointment.status.in_(ACTIVE), Appointment.resource_id == bay,
        Appointment.ends_at >= (now - timedelta(days=1)).astimezone(timezone.utc),
        Appointment.starts_at <= horizon_end.astimezone(timezone.utc)).order_by(Appointment.starts_at).all()
    busy_jobs = jobs_for(db, busy_rows)
    busy = [AppointmentWindow(aware(a.starts_at), aware(a.ends_at), a.resource_id,
                              a.travel_before_minutes or 0, a.buffer_after_minutes or 0) for a in busy_rows]
    days = []
    for offset in range(DAYS_AHEAD):
        current = today + timedelta(days=offset)
        if current.weekday() not in hours:
            continue
        starts = free_starts(current, hours, busy, bay, length * 60, travel_used, buffer_used, now)
        days.append({"iso": current.isoformat(), "short": current.strftime("%a"), "date": current.strftime("%b %-d"),
                     "label": day_label(current, today), "free": len(starts), "starts": starts})
    chosen = next((d for d in days if d["iso"] == day), None) or next((d for d in days if d["free"]), None) \
        or (days[0] if days else None)
    slots = []
    if chosen:
        for start in chosen["starts"]:
            end = start + timedelta(hours=length)
            slots.append({"label": start.strftime("%-I:%M %p"), "starts_at": start.isoformat(), "ends_at": end.isoformat()})
    booked_that_day = []
    if chosen:
        for a in busy_rows:
            if local(a.starts_at).date().isoformat() == chosen["iso"]:
                job = busy_jobs.get(a.repair_order_id)
                who = job.customer if job else None
                booked_that_day.append({"time": f"{clock(a.starts_at)} – {clock(a.ends_at)}",
                                        "who": customer_name(who), "job_id": a.repair_order_id})
    existing = db.query(Appointment).filter(Appointment.repair_order_id == order.id,
                                            Appointment.status.in_(ACTIVE)).order_by(Appointment.starts_at).all()
    return {
        "order_id": order.id,
        "customer": customer_name(order.customer),
        "phone": phone_text(order.customer.phone) if order.customer else "",
        "vehicle": order.vehicle.label if order.vehicle else "Vehicle details not given yet",
        "what": what_for(order) or "No concern written on the job yet",
        "days": [{k: v for k, v in d.items() if k != "starts"} for d in days],
        "day": chosen["iso"] if chosen else "",
        "day_label": chosen["label"] if chosen else "",
        "slots": slots,
        "booked_that_day": booked_that_day,
        "length": length, "lengths": LENGTHS,
        "bay": bay, "bays": [(b, bay_name(b)) for b in bays],
        "where": where, "travel": travel, "travel_choices": TRAVEL_CHOICES,
        "buffer": buffer,
        "existing": [{"id": a.id, "when": local(a.starts_at).strftime("%a %b %-d, %-I:%M %p"),
                      "status": STATUS_TEXT.get(a.status, a.status)} for a in existing],
    }
