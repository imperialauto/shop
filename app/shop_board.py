"""The shop TV board: today's jobs, money in today against the daily goal, and the shop to-do list.

Read on a TV across the bay, so it carries first names and vehicles only: no phone numbers,
no addresses, no prices per job. The to-do list lives in imperial_tasks under its own
category (BOARD_CATEGORY), so no new table is needed and it stays off the owner's personal
to-do, the Master Task Tracker and Needs attention.

A to-do comes from one of three places, told apart by `source`:
* SOURCE_MANUAL: typed on the phone page;
* SOURCE_TASKS: copied from the owner's Tasks page by the assistant, `external_id` = that task's key;
* SOURCE_ALERT: a customer message, missed call, booking or decision that needs the owner
  (a dispatch item in status needs_owner), `external_id` = "item:<id>". These appear and move
  to Completed by themselves as the item is handled, when SHOP_BOARD_ALERTS is on.
Done items stay on the board under "Completed today".
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import event, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import business_settings
from app.next_action import days_since
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.imperial_task_model import BOARD_CATEGORY, ImperialTask, ImperialTaskAudit
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.models import Communication, Customer, JobDeposit, LineItem, PartsOrder, Payment, RepairOrder, User, Vehicle
from app.ro_status_policy import CLOSED_STATUSES, is_open

log = logging.getLogger(__name__)
GOAL_KEY = "daily_sales_goal"
GOAL_MODE_KEY = "daily_goal_mode"  # "fixed" (the goal above) or "bills" (what the bills need a day, when more than 0)
GOAL_MODES = ("fixed", "bills")
CLOCK_KEY = "board_job_clock"  # time actually worked per job, started and stopped on the phone page
TIMER_KEY = "board_timers"  # running job and test timers shown big on the TV
TIMER_MAX_MINUTES = 480
TIMER_MAX_RUNNING = 6
TIMER_KEEP_AFTER = timedelta(minutes=30)  # a finished timer stays up (flashing) this long unless stopped
LOT_KEY = "board_on_lot"  # which open jobs' cars are physically at the shop; board view only
WORK_STAGES = ("in_progress", "diagnosing", "approval_received", "waiting_parts", "complete")
GOAL_DEFAULT = 500  # Jae's figure (Nov 21, 2026): $500 a day including parts
GOAL_MAX = 100000
OPEN_TODO = ("open", "in_progress", "blocked")
DONE = "done_verified"
MAX_JOBS = 12
MAX_TODOS = 60
MAX_DONE_SHOWN = 12
SOURCE_MANUAL = "shop_board"
SOURCE_TASKS = "tasks_page"
SOURCE_ALERT = "dispatch_alert"
NEEDS_OWNER = "needs_owner"
RECONCILE_EVERY = 300  # seconds: a catch-up pass in case an alert change was missed

# What kind of alert a dispatch item is, in shop words. First match wins.
ALERT_KINDS = (
    ("safety", "Safety text"),
    ("missed_call", "Missed call"),
    ("approval", "Customer decision"),
    ("booking", "New booking"),
    ("estimate", "Estimate request"),
    ("estimate_pricing", "Price question"),
    ("pricing", "Price question"),
    ("arrival", "Customer is here"),
    ("towing", "Tow request"),
    ("scheduling", "Scheduling"),
    ("status", "Status check"),
    ("reference_help", "Which job?"),
)

# Shop-floor words for each job stage, and the order the board lists them in.
STAGES = {
    "in_progress": ("In progress", 0),
    "diagnosing": ("Diagnosing", 1),
    "approval_received": ("Approved, ready to start", 2),
    "intake": ("Checked in", 3),
    "waiting_parts": ("Parts ordered", 4),
    "waiting_approval": ("Waiting on approval", 5),
    "complete": ("Ready for pickup", 6),
}


def _actor(user) -> str:
    """Who changed a board setting: a signed-in user, or a plain actor string such as "agent:7" for the assistant."""
    return user if isinstance(user, str) else f"user:{user.id}"


def now_local() -> datetime:
    return datetime.now(PHOENIX)


def daily_goal() -> int:
    try:
        value = int(float(business_settings.raw(GOAL_KEY) or ""))
    except (TypeError, ValueError):
        return GOAL_DEFAULT
    return value if 1 <= value <= GOAL_MAX else GOAL_DEFAULT


def _local(value):
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(PHOENIX)


def _who(customer) -> str:
    """First name only: customers can see the TV (Jae, Nov 22, 2026)."""
    if not customer:
        return ""
    first = (customer.first_name or "").strip()
    last = (customer.last_name or "").strip().rstrip("-").strip()
    if not first or first.lower().startswith("unknown") or first[0].isdigit():
        return ""  # placeholder names like "2013 Ford Explorer Overheating" aren't a person
    return first.split()[0][:24]


def _job(order, *, time_label=None, mobile=False):
    label, rank = STAGES.get(order.status, (order.status.replace("_", " ").capitalize(), 9))
    if order.status == "approval_received" and not any(li.approval == "approved" for li in order.line_items):
        label = "Customer declined, call them"  # every quoted line was declined: nothing to start
    tech = order.assigned_tech
    return {
        "id": order.id,
        "po": order.po_number,
        "time": time_label,
        "mobile": mobile,
        "who": _who(order.customer),
        "vehicle": order.vehicle.label if order.vehicle else "Vehicle details not given yet",
        "stage": label,
        "stage_key": order.status,
        "rank": rank,
        "tech": ((tech.full_name or tech.username).split()[0] if tech and (tech.full_name or tech.username) else ""),
    }


def lot_marks(db: Session | None = None) -> dict:
    """{"here": {ro_id: "YYYY-MM-DD" it arrived}, "away": [ro_id, ...]} as last set on the phone page.
    Read straight from the table when a session is given, so a change is never lost to the settings cache."""
    text = None
    if db is not None:
        row = db.get(business_settings.BusinessSetting, LOT_KEY)
        text = row.value if row is not None else None
    if text is None:
        text = business_settings.raw(LOT_KEY)
    try:
        data = json.loads(text or "{}")
    except (TypeError, ValueError):
        data = {}
    here = {int(k): str(v)[:10] for k, v in (data.get("here") or {}).items() if str(k).isdigit()}
    away = {int(v) for v in (data.get("away") or []) if str(v).isdigit()}
    return {"here": here, "away": away}


def _is_here(order, marks) -> bool:
    if order.id in marks["here"]:
        return True
    return order.status in WORK_STAGES and order.id not in marks["away"]


def car_here(order, marks=None) -> bool:
    """Whether Home and the job page show this job's car as at the shop (same rule as the TV board)."""
    if marks is None:
        from sqlalchemy.orm import object_session
        db = object_session(order)
        try:
            marks = lot_marks(db)
        except SQLAlchemyError:
            if db is not None:
                db.rollback()
            marks = {"here": {}, "away": set()}
    return _is_here(order, marks)


def _arrived(order, marks) -> datetime | None:
    mark = marks["here"].get(order.id)
    if mark:
        try:
            return datetime.strptime(mark, "%Y-%m-%d").replace(tzinfo=PHOENIX)
        except ValueError:
            pass
    return _local(order.created_at) if order.created_at else None


def open_orders(db: Session) -> list:
    """Open jobs, one per vehicle (the newest), so a duplicate job doesn't show a car twice."""
    orders = db.query(RepairOrder).filter(RepairOrder.status.notin_(CLOSED_STATUSES)).order_by(RepairOrder.id.desc()).all()
    out, cars = [], set()
    for order in orders:
        if order.vehicle_id in cars:
            continue
        cars.add(order.vehicle_id)
        out.append(order)
    return out


def jobs_today(db: Session, now: datetime) -> list[dict]:
    """Cars that are physically at the shop, by stage, with how many days each has been here.

    A job counts as here when it was marked here on the phone page, or work on it has started
    (unless it was marked gone). Booked visits for cars not here yet show under Coming in."""
    marks = lot_marks(db)
    clocks = job_clocks(db)
    today = now.date()
    out = []
    for order in open_orders(db):
        if not _is_here(order, marks):
            continue
        job = _job(order)
        arrived = _arrived(order, marks)
        job["days"] = max(0, (today - arrived.astimezone(PHOENIX).date()).days) if arrived else None
        job.update(_time_vs_book(db, order, clocks.get(order.id), now))
        out.append(job)
    out.sort(key=lambda job: (job["rank"], -(job["days"] or 0), -job["id"]))
    return out[:MAX_JOBS]


# ---------------------------------------------------------------- book time vs time worked

def book_hours(db: Session, ro_id: int) -> float:
    """Book labor hours on the job: labor lines not declined (quantity is hours)."""
    lines = db.query(LineItem).filter(LineItem.repair_order_id == ro_id, LineItem.item_type == "labor").all()
    return round(sum(max(0.0, line.quantity or 0.0) for line in lines if line.approval != "declined"), 1)


# Each job's clock is kept per worker, so two people on one car each count, and the hours compare with book
# labor (person-hours). {"<ro_id>": {"by": {"<who>": {"on": iso or null, "spent": seconds}},
#                                    "days": {"YYYY-MM-DD": seconds closed that Phoenix day}}}
# <who> is "u<user id>" for a signed-in worker, or "shop" for the assistant and for clocks saved before
# clocks were per worker. A job keeps its clock for CLOCK_KEEP_DAYS after pickup so the job page can still
# show clocked against billed hours.
SHOP_CLOCK = "shop"
CLOCK_KEEP_DAYS = 45


def _parse_clock(value) -> dict | None:
    if not isinstance(value, dict):
        return None

    def entry(raw):
        on = datetime.fromisoformat(raw["on"]) if raw.get("on") else None
        if on is not None and on.tzinfo is None:
            on = on.replace(tzinfo=timezone.utc)
        return {"on": on, "spent": max(0, int(raw.get("spent") or 0))}

    try:
        if isinstance(value.get("by"), dict):
            by = {str(k)[:24]: entry(v) for k, v in value["by"].items() if isinstance(v, dict)}
        else:  # saved before clocks were per worker
            by = {SHOP_CLOCK: entry(value)}
        days = {str(k)[:10]: max(0, int(v or 0)) for k, v in (value.get("days") or {}).items()}
    except (TypeError, ValueError, AttributeError):
        return None
    running = [w["on"] for w in by.values() if w["on"]]
    return {"on": min(running) if running else None, "spent": sum(w["spent"] for w in by.values()),
            "by": by, "days": days}


def job_clocks(db: Session, text: str | None = None) -> dict:
    """{ro_id: {"on": earliest running start or None, "spent": closed seconds, "by": per worker, "days": per day}}."""
    if text is None:
        row = db.get(business_settings.BusinessSetting, CLOCK_KEY)
        text = row.value if row is not None else None
    try:
        data = json.loads(text or "{}")
    except (TypeError, ValueError):
        data = {}
    out = {}
    for key, value in (data.items() if isinstance(data, dict) else []):
        clock = _parse_clock(value) if str(key).isdigit() else None
        if clock is not None:
            out[int(key)] = clock
    return out


def _entry_seconds(entry: dict, now: datetime) -> int:
    running = int((now - entry["on"]).total_seconds()) if entry["on"] else 0
    return entry["spent"] + max(0, running)


def worked_seconds(clock: dict | None, now: datetime, who: str | None = None) -> int:
    """Time worked on a job, every worker together (person-time), or one worker's when `who` is given."""
    if not clock:
        return 0
    by = clock.get("by") or {SHOP_CLOCK: {"on": clock["on"], "spent": clock["spent"]}}
    if who is not None:
        return _entry_seconds(by[who], now) if who in by else 0
    return sum(_entry_seconds(entry, now) for entry in by.values())


def _split_by_day(start: datetime, end: datetime) -> dict:
    """Seconds from start to end, split at Phoenix midnights: {"YYYY-MM-DD": seconds}."""
    out = {}
    current = start.astimezone(PHOENIX)
    end = end.astimezone(PHOENIX)
    while current < end:
        midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        stop = min(end, midnight)
        key = current.strftime("%Y-%m-%d")
        out[key] = out.get(key, 0) + int((stop - current).total_seconds())
        current = stop
    return out


def clocked_seconds_on(clocks: dict, day, now: datetime) -> int:
    """Seconds clocked on every job on one Phoenix day, running clocks included up to now."""
    key = day.strftime("%Y-%m-%d")
    total = 0
    for clock in clocks.values():
        total += clock["days"].get(key, 0)
        for entry in clock["by"].values():
            if entry["on"] is not None:
                total += _split_by_day(entry["on"], now).get(key, 0)
    return total


def worker_key(user) -> str:
    """Whose clock a change is: a signed-in worker's own, or the shared one for the assistant ("agent:7")."""
    return SHOP_CLOCK if isinstance(user, str) else f"u{user.id}"


def _time_vs_book(db: Session, order, clock: dict | None, now: datetime) -> dict:
    book = book_hours(db, order.id)
    worked = worked_seconds(clock, now) / 3600
    return {"book": book, "worked": round(worked, 1), "clock_on": bool(clock and clock["on"]),
            "over_book": bool(book and worked > book)}


def _locked_clock_text(db: Session) -> str | None:
    """The saved clocks, with the row locked until commit on Postgres, so two people tapping at once don't
    overwrite each other's clock."""
    row = (db.query(business_settings.BusinessSetting)
           .filter(business_settings.BusinessSetting.key == CLOCK_KEY).with_for_update().first())
    return row.value if row is not None else None


def _clock_json(clock: dict) -> dict:
    return {"by": {who: {"on": e["on"].isoformat() if e["on"] else None, "spent": e["spent"]}
                   for who, e in sorted(clock["by"].items())},
            "days": dict(sorted(clock["days"].items()))}


def set_clock(db: Session, user: User, ro_id: int, on: bool, now: datetime | None = None,
              only_mine: bool = False) -> bool:
    """Start or stop the work clock on a job. Changes only the clock, never the job.

    Starting starts the worker's own clock (the shared one for the assistant). Stopping stops the worker's own
    clock when only_mine (the job page), otherwise every clock running on the job (the phone page and the
    assistant, which stop the job). A picked-up job can still be stopped but not started."""
    order = db.get(RepairOrder, ro_id)
    if order is None or (on and order.status == "delivered"):
        return False
    now = (now or now_local()).astimezone(timezone.utc)
    clocks = job_clocks(db, _locked_clock_text(db))
    clock = clocks.setdefault(ro_id, {"on": None, "spent": 0, "by": {}, "days": {}})
    me = worker_key(user)
    if on:
        entry = clock["by"].setdefault(me, {"on": None, "spent": 0})
        if entry["on"] is None:
            entry["on"] = now
    else:
        for who, entry in clock["by"].items():
            if entry["on"] is None or (only_mine and who != me):
                continue
            for day, seconds in _split_by_day(entry["on"], now).items():
                clock["days"][day] = clock["days"].get(day, 0) + seconds
            entry["spent"] = _entry_seconds(entry, now)
            entry["on"] = None
    keep_after = (now - timedelta(days=CLOCK_KEEP_DAYS)).replace(tzinfo=None)
    kept = {o.id for o in db.query(RepairOrder.id).filter(
        (RepairOrder.status != "delivered") | (RepairOrder.updated_at >= keep_after))}
    value = json.dumps({str(k): _clock_json(v) for k, v in sorted(clocks.items())
                        if k in kept or any(e["on"] for e in v["by"].values())})
    if business_settings.save(db, CLOCK_KEY, value, _actor(user)):
        db.commit()
        business_settings.clear_cache()
    return True


# ---------------------------------------------------------------- pickup and waiting on the customer

def ready_for_pickup(db: Session) -> list[dict]:
    """Finished cars still here: first name and vehicle, for a banner across the TV."""
    marks = lot_marks(db)
    return [{"id": o.id, "who": _who(o.customer), "vehicle": o.vehicle.label if o.vehicle else ""}
            for o in open_orders(db) if o.status == "complete" and _is_here(o, marks)]


def waiting_on_customer(db: Session, now: datetime) -> list[dict]:
    """Open jobs with lines still waiting on the customer's yes, longest wait first.

    The wait counts from when the approval was last sent to them (approval_sent_at), not from when
    the lines were written. A job with nothing pending isn't waiting on anyone, whatever its status says."""
    out = []
    for order in open_orders(db):
        pending = [line for line in order_lines(db, order.id) if line.approval == "pending"]
        if not pending:
            continue
        since = approval_sent_at(db, order, pending)
        hours = max(0, int((now - since).total_seconds() // 3600)) if since else None
        out.append({"id": order.id, "who": _who(order.customer), "customer_id": order.customer_id,
                    "phone": bool(order.customer and order.customer.phone),
                    "vehicle": order.vehicle.label if order.vehicle else "", "hours": hours,
                    "wait": _wait_label(hours), "stale": bool(hours and hours >= STALE_WAIT_DAYS * 24)})
    out.sort(key=lambda w: -(w["hours"] or 0))
    return out


STALE_WAIT_DAYS = 30  # past this, Home says so plainly: call them or close the job


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def approval_sent_at(db: Session, order, pending=None) -> datetime | None:
    """When the customer was last sent this job's approval link: the latest of the link text going out
    (dispatch_outbound for the job's LINK item), the owner approving that text, or a logged text that
    carries the job's link. Falls back to the newest pending line, then the job's own dates."""
    from app.approval_links import conversation_key

    found = []
    items = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(order.id)).all()
    ids = [item.id for item in items]
    if ids:
        found += [row.updated_at or row.created_at for row in db.query(DispatchOutbound).filter(
            DispatchOutbound.item_id.in_(ids), DispatchOutbound.status == "sent")]
        found += [item.decided_at for item in items if item.status in ("approved", "closed") and item.decided_at]
    if order.public_token:
        found += [c.created_at for c in db.query(Communication).filter(
            Communication.customer_id == order.customer_id, Communication.direction == "outbound",
            Communication.body.contains(order.public_token))]
    found = [_aware(when) for when in found if when is not None]
    if found:
        return max(found)
    if pending is None:
        pending = [line for line in order_lines(db, order.id) if line.approval == "pending"]
    lines = [_aware(when) for line in pending for when in (line.created_at, line.changed_at) if when is not None]
    if lines:
        return max(lines)
    return _aware(order.updated_at or order.created_at)


def order_lines(db: Session, ro_id: int) -> list:
    return db.query(LineItem).filter(LineItem.repair_order_id == ro_id).all()


def _wait_label(hours: int | None) -> str:
    if hours is None:
        return ""
    if hours < 1:
        return "Just sent"
    if hours < 48:
        return f"{hours} hr"
    return f"{hours // 24} days"


def lot_list(db: Session) -> list[dict]:
    """Every open job with whether the board counts its car as here, for the phone page."""
    marks = lot_marks(db)
    rows = []
    clocks = job_clocks(db)
    now = now_local()
    for order in open_orders(db):
        job = _job(order)
        job["here"] = _is_here(order, marks)
        job.update(_time_vs_book(db, order, clocks.get(order.id), now))
        job["marked"] = order.id in marks["here"] or order.id in marks["away"]
        rows.append(job)
    rows.sort(key=lambda job: (not job["here"], job["rank"], -job["id"]))
    return rows


def set_here(db: Session, user: User, ro_id: int, here: bool, now: datetime | None = None) -> bool:
    """Mark a job's car as at the shop or gone. Changes only what the board shows, never the job."""
    order = db.get(RepairOrder, ro_id)
    if order is None or not is_open(order):
        return False
    marks = lot_marks(db)
    if here:
        marks["here"].setdefault(ro_id, (now or now_local()).strftime("%Y-%m-%d"))
        marks["away"].discard(ro_id)
    else:
        marks["here"].pop(ro_id, None)
        marks["away"].add(ro_id)
    open_ids = {o.id for o in db.query(RepairOrder.id).filter(RepairOrder.status.notin_(CLOSED_STATUSES))}
    value = json.dumps({"here": {str(k): v for k, v in sorted(marks["here"].items()) if k in open_ids},
                        "away": sorted(i for i in marks["away"] if i in open_ids)})
    if business_settings.save(db, LOT_KEY, value, _actor(user)):
        db.commit()
        business_settings.clear_cache()
    return True


# ---------------------------------------------------------------- job and test timers

def _timer_rows(db: Session) -> list[dict]:
    row = db.get(business_settings.BusinessSetting, TIMER_KEY)
    try:
        data = json.loads(row.value if row is not None and row.value else "[]")
    except (TypeError, ValueError):
        data = []
    rows = []
    for t in data if isinstance(data, list) else []:
        try:
            ends = datetime.fromisoformat(t["ends"])
            rows.append({"id": str(t["id"])[:16], "label": str(t.get("label") or "Timer")[:60], "ends": ends,
                         "minutes": int(t.get("minutes") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _save_timers(db: Session, user: User, rows: list[dict]) -> None:
    value = json.dumps([{"id": t["id"], "label": t["label"], "minutes": t["minutes"], "ends": t["ends"].isoformat()}
                        for t in rows])
    if business_settings.save(db, TIMER_KEY, value, _actor(user)):
        db.commit()
        business_settings.clear_cache()


def timers(db: Session, now: datetime) -> list[dict]:
    """Running timers, soonest first. One that ran out shows as time up for a while, then drops off."""
    out = []
    for t in sorted(_timer_rows(db), key=lambda t: t["ends"]):
        if now - t["ends"] > TIMER_KEEP_AFTER:
            continue
        left = int((t["ends"] - now).total_seconds())
        out.append({"id": t["id"], "label": t["label"], "minutes": t["minutes"], "left": left, "up": left <= 0,
                    "ends_ms": int(t["ends"].timestamp() * 1000), "ends_at": t["ends"].astimezone(PHOENIX).strftime("%-I:%M %p")})
    return out


def start_timer(db: Session, user: User, label: str, minutes, now: datetime | None = None) -> dict:
    label = " ".join((label or "").split())[:60] or "Timer"
    try:
        minutes = int(float(str(minutes).strip()))
    except (TypeError, ValueError):
        raise ValueError("Enter the minutes as a number, like 15.") from None
    if not 1 <= minutes <= TIMER_MAX_MINUTES:
        raise ValueError(f"Pick between 1 and {TIMER_MAX_MINUTES} minutes.")
    now = (now or now_local()).astimezone(timezone.utc)
    rows = [t for t in _timer_rows(db) if now - t["ends"] <= TIMER_KEEP_AFTER]
    if len(rows) >= TIMER_MAX_RUNNING:
        raise ValueError(f"{TIMER_MAX_RUNNING} timers are already up. Stop one first.")
    timer = {"id": uuid4().hex[:12], "label": label, "minutes": minutes, "ends": now + timedelta(minutes=minutes)}
    _save_timers(db, user, rows + [timer])
    return timer


def change_timer(db: Session, user: User, timer_id: str, operation: str, now: datetime | None = None) -> bool:
    """stop: take it off the TV. plus5: add five minutes (from now if it already ran out)."""
    now = (now or now_local()).astimezone(timezone.utc)
    rows = _timer_rows(db)
    found = next((t for t in rows if t["id"] == timer_id), None)
    if found is None:
        return False
    if operation == "stop":
        rows.remove(found)
    elif operation == "plus5":
        found["ends"] = max(found["ends"], now) + timedelta(minutes=5)
        found["minutes"] += 5
    else:
        raise ValueError("Unknown action.")
    _save_timers(db, user, [t for t in rows if now - t["ends"] <= TIMER_KEEP_AFTER])
    return True


def coming_in(db: Session, now: datetime) -> list[dict]:
    """Booked visits today and tomorrow for cars that aren't here yet."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    marks = lot_marks(db)
    visits = db.query(Appointment).filter(
        Appointment.status.in_(("tentative", "confirmed")),
        Appointment.starts_at >= max(now - timedelta(hours=2), start).astimezone(timezone.utc),
        Appointment.starts_at < (start + timedelta(days=2)).astimezone(timezone.utc),
        Appointment.ends_at > now.astimezone(timezone.utc),  # a visit whose time is over isn't coming in any more
    ).order_by(Appointment.starts_at).limit(20).all()
    out, seen = [], set()
    for visit in visits:
        order = db.get(RepairOrder, visit.repair_order_id)
        if order is None or order.id in seen or order.status == "delivered" or _is_here(order, marks):
            continue
        seen.add(order.id)
        when = _local(visit.starts_at)
        out.append({"day": "Today" if when.date() == now.date() else "Tomorrow", "time": when.strftime("%-I:%M %p"),
                    "mobile": visit.location == "mobile", "who": _who(order.customer),
                    "ro_id": order.id, "customer_id": order.customer_id,
                    "phone": bool(order.customer and order.customer.phone),
                    "vehicle": order.vehicle.label if order.vehicle else "Vehicle not given yet",
                    "tentative": visit.status == "tentative"})
    return out


def waiting_on_you(db: Session) -> dict:
    """Counts only: customer texts and missed calls that need the owner, and estimates waiting on a customer's yes."""
    texts = calls = 0
    for (reasons,) in db.query(DispatchItem.approval_reasons).filter(DispatchItem.status == NEEDS_OWNER):
        try:
            missed = "missed_call" in json.loads(reasons or "[]")
        except (TypeError, ValueError):
            missed = False
        calls += missed
        texts += not missed
    pending = {ro for (ro,) in db.query(LineItem.repair_order_id).join(RepairOrder).filter(
        LineItem.approval == "pending", RepairOrder.status.notin_(CLOSED_STATUSES))}
    out = {"texts": texts, "calls": calls, "estimates": len(pending)}
    out["total"] = sum(out.values())
    return out


def parts_coming(db: Session) -> list[dict]:
    """Parts ordered and not received yet, for open jobs. No prices on the TV."""
    rows = db.query(PartsOrder).filter(PartsOrder.status == "ordered").order_by(PartsOrder.ordered_at).limit(20).all()
    out = []
    for part in rows:
        order = db.get(RepairOrder, part.repair_order_id) if part.repair_order_id else None
        if order is not None and order.status == "delivered":
            continue
        out.append({"part": (part.description or part.part_number or "Part")[:60], "vendor": (part.vendor or "")[:30],
                    "vehicle": order.vehicle.label if order is not None and order.vehicle else "",
                    "ordered": _local(part.ordered_at).strftime("%a %-m/%-d") if part.ordered_at else ""})
    return out


def sales_today(db: Session, now: datetime) -> dict:
    """Money in today (Phoenix day) against the same target Home shows, so the TV and the dashboard always agree:
    job payments, job deposits not on an invoice yet (Zelle and the rest), and money marked "Got it"."""
    from app.routes.finance import home_goal, phoenix_today, sales_in  # late import: finance pulls in the web routes
    day = now.astimezone(PHOENIX).date() if now.tzinfo else now.date()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    since = start.astimezone(timezone.utc).replace(tzinfo=None)  # paid_at is saved as UTC without a zone
    until = (start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    total = sales_in(db, day, day + timedelta(days=1))
    count = (db.query(Payment).filter(Payment.paid_at >= since, Payment.paid_at < until).count()
             + db.query(JobDeposit).filter(JobDeposit.received_at >= since, JobDeposit.received_at < until,
                                           JobDeposit.removed_at.is_(None), JobDeposit.payment_id.is_(None)).count())
    home = home_goal(db) if day == phoenix_today() else None
    goal = home["target"] if home else daily_goal()
    return {"total": total, "goal": goal, "count": count,
            "percent": max(0, min(100, int(total * 100 / goal))) if goal else 0,
            "left": round(max(goal - total, 0), 2), "hit": total >= goal}


def _kind(task: ImperialTask) -> str:
    return {SOURCE_ALERT: "alert", SOURCE_TASKS: "task"}.get(task.source, "manual")


def todo_json(task: ImperialTask) -> dict:
    done = task.status == DONE
    return {"id": task.id, "title": task.title, "urgent": task.priority == "urgent", "kind": _kind(task),
            "task_key": task.external_id if task.source == SOURCE_TASKS else None,
            "done": done, "done_at": _local(task.updated_at).isoformat() if done and task.updated_at else None,
            "added_at": _local(task.created_at).isoformat() if task.created_at else None,
            "days_old": days_since(task.created_at) if not done else 0}


def todos(db: Session, now: datetime) -> dict:
    """Open to-dos (urgent, then alerts, then oldest first) and what was completed today."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    rows = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.status.in_(OPEN_TODO),
    ).order_by(ImperialTask.priority != "urgent", ImperialTask.source != SOURCE_ALERT, ImperialTask.id).limit(MAX_TODOS).all()
    open_count = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.status.in_(OPEN_TODO)).count()
    done = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.status == DONE,
        ImperialTask.updated_at >= start,
    ).order_by(ImperialTask.updated_at.desc()).limit(MAX_DONE_SHOWN).all()
    done_count = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.status == DONE,
        ImperialTask.updated_at >= start).count()
    return {"open": [todo_json(t) for t in rows], "open_count": open_count,
            "done": [todo_json(t) for t in done], "done_count": done_count}


def snapshot(db: Session, user: User, now: datetime | None = None) -> dict:
    """Everything the board shows. Each part fails on its own, so one problem doesn't blank the TV."""
    from app.routes.imperial_tasks import enabled

    now = (now or now_local()).astimezone(PHOENIX)
    data = {"now": now, "jobs": None, "sales": None, "todos": None, "todos_on": enabled(),
            "show_money": user.role == "owner", "coming": None, "waiting": None, "parts": None, "timers": None,
            "pickup": None, "on_customer": None}
    for key, read in (("jobs", jobs_today), ("coming", coming_in), ("timers", timers), ("on_customer", waiting_on_customer)):
        try:
            data[key] = read(db, now)
        except SQLAlchemyError:
            db.rollback()
    for key, read in (("waiting", waiting_on_you), ("parts", parts_coming), ("pickup", ready_for_pickup)):
        try:
            data[key] = read(db)
        except SQLAlchemyError:
            db.rollback()
    if data["show_money"]:
        try:
            data["sales"] = sales_today(db, now)
        except SQLAlchemyError:
            db.rollback()
    if data["todos_on"]:
        maybe_reconcile(db)
        try:
            data["todos"] = todos(db, now)
        except SQLAlchemyError:
            db.rollback()
    return data


def _clean_title(title: str) -> str:
    title = " ".join((title or "").split())
    if not 2 <= len(title) <= 200:
        raise ValueError("Type a to-do between 2 and 200 characters.")
    return title


def add_todo(db: Session, user: User | None, title: str, urgent: bool = False, task_key: str | None = None,
             actor: str | None = None, log_change=None) -> tuple[ImperialTask, bool]:
    """Add a to-do; returns (task, created). With task_key (a Tasks-page task), adding the same key again
    returns the to-do already on the board instead of a second copy."""
    title = _clean_title(title)
    actor = actor or f"user:{user.id}"
    if task_key:
        task_key = task_key.strip()[:128]
        existing = db.query(ImperialTask).filter_by(source=SOURCE_TASKS, external_id=task_key).first()
        if existing is not None:
            if existing.category != BOARD_CATEGORY:
                raise ValueError("That task key is already used by something else.")
            return existing, False
    event_id = uuid4().hex
    task = ImperialTask(title=title, source=SOURCE_TASKS if task_key else SOURCE_MANUAL,
                        external_id=task_key or event_id, category=BOARD_CATEGORY,
                        priority="urgent" if urgent else "normal", status="open")
    db.add(task)
    db.flush()
    db.add(ImperialTaskAudit(task_id=task.id, actor=actor, action="created", event_id=event_id))
    if log_change:
        log_change(task)
    db.commit()
    return task, True


def find_todo(db: Session, todo_id: int | None = None, task_key: str | None = None) -> ImperialTask | None:
    query = db.query(ImperialTask).filter(ImperialTask.category == BOARD_CATEGORY)
    if todo_id:
        return query.filter(ImperialTask.id == todo_id).first()
    if task_key:
        return query.filter(ImperialTask.source == SOURCE_TASKS, ImperialTask.external_id == task_key.strip()).first()
    return None


def set_done(db: Session, user: User | None, task_id: int, done: bool, actor: str | None = None,
             log_change=None) -> bool:
    """Tick off (or bring back) one board to-do. Other kinds of task can't be changed from here.

    Ticking off an alert also closes its customer item in the dashboard, so the two agree.
    An alert can't be brought back from the board: a new message from the customer reopens it."""
    task = db.query(ImperialTask).filter_by(id=task_id, category=BOARD_CATEGORY).first()
    if task is None:
        return False
    actor = actor or f"user:{user.id}"
    if task.source == SOURCE_ALERT and not done:
        raise ValueError("That one came from a customer. It comes back by itself if they message again.")
    if task.source == SOURCE_ALERT and done:
        if user is not None and user.role != "owner":
            raise ValueError("Only the owner can clear a customer alert.")
        _close_alert_item(db, task, actor)
    task.status = DONE if done else "open"
    task.updated_at = now_local().astimezone(timezone.utc)
    db.add(ImperialTaskAudit(task_id=task.id, actor=actor,
                             action="complete" if done else "reopen", event_id=uuid4().hex))
    if log_change:
        log_change(task)
    db.commit()
    return True


def _close_alert_item(db: Session, task: ImperialTask, actor: str) -> None:
    from app.routes.dispatch import audit

    item_id = _alert_item_id(task.external_id)
    item = db.get(DispatchItem, item_id) if item_id else None
    if item is not None and item.status == NEEDS_OWNER:
        item.status = "closed"
        item.updated_at = datetime.now(timezone.utc)
        audit(db, item.id, actor[:64], "board_done")


def goal_mode() -> str:
    """Which goal Home, the scoreboard and the TV measure against: "fixed" (default) or "bills"."""
    return "bills" if (business_settings.raw(GOAL_MODE_KEY) or "").strip().lower() == "bills" else "fixed"


def save_goal(db: Session, user: User, value: str, mode: str | None = None) -> int:
    try:
        goal = int(float((value or "").replace("$", "").replace(",", "").strip()))
    except ValueError:
        raise ValueError("Enter the daily goal as a dollar amount, like 500.") from None
    if not 1 <= goal <= GOAL_MAX:
        raise ValueError("Enter a daily goal between $1 and $100,000.")
    if mode is not None and mode not in GOAL_MODES:
        raise ValueError("Pick the fixed goal or what the bills need.")
    changed = business_settings.save(db, GOAL_KEY, str(goal), f"owner:{user.id}")
    if mode is not None:
        changed = business_settings.save(db, GOAL_MODE_KEY, mode, f"owner:{user.id}") or changed
    if changed:
        db.commit()
        business_settings.clear_cache()
    return goal


# ---------------------------------------------------------------- alerts that need the owner

def alerts_enabled() -> bool:
    """SHOP_BOARD_ALERTS=true puts customer items that need the owner on the board by themselves."""
    from app.routes.imperial_tasks import enabled

    return os.getenv("SHOP_BOARD_ALERTS", "").strip().lower() in ("1", "true", "yes", "on") and enabled()


def alerts_since() -> datetime | None:
    """SHOP_BOARD_ALERTS_SINCE (ISO time): only items with activity at or after it get a new
    to-do, so turning alerts on doesn't dump the old backlog onto the board."""
    raw = os.getenv("SHOP_BOARD_ALERTS_SINCE", "").strip()
    if not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        log.warning("shop_board_alerts_since_invalid")
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _is_new(item: DispatchItem, since: datetime | None) -> bool:
    if since is None:
        return True
    when = item.updated_at or item.created_at
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when >= since


def _alert_key(item_id: int) -> str:
    return f"item:{item_id}"


def _alert_item_id(key: str | None) -> int | None:
    try:
        return int((key or "").split("item:", 1)[1])
    except (IndexError, ValueError):
        return None


def alert_kind(item: DispatchItem) -> str:
    try:
        reasons = set(json.loads(item.approval_reasons or "[]"))
    except (TypeError, ValueError):
        reasons = set()
    marks = {item.urgency, item.category} | reasons
    for key, label in ALERT_KINDS:
        if key in marks:
            return label
    return "Customer text"


def alert_title(db: Session, item: DispatchItem) -> str:
    """Kind, first name and last initial, vehicle. Never the phone number or what they wrote."""
    who = _who(db.get(Customer, item.customer_id)) if item.customer_id else ""
    vehicle = db.get(Vehicle, item.vehicle_id) if item.vehicle_id else None
    car = vehicle.label if vehicle is not None else (item.vehicle_text or "").strip()
    if car == "Vehicle details not given yet":
        car = ""
    detail = ", ".join(p for p in (who, car[:60]) if p) or "new customer"
    return f"{alert_kind(item)}: {detail}"[:200]


def sync_alert(db: Session, item: DispatchItem, actor: str = "system:alerts") -> str:
    """Make the board agree with one dispatch item. Returns what changed ("added", "done", "reopened", "")."""
    key = _alert_key(item.id)
    task = db.query(ImperialTask).filter_by(source=SOURCE_ALERT, external_id=key).first()
    now = datetime.now(timezone.utc)
    needs_owner = item.status == NEEDS_OWNER
    urgent = item.urgency not in (None, "", "normal")
    if task is None:
        if not needs_owner or not _is_new(item, alerts_since()):
            return ""
        task = ImperialTask(title=alert_title(db, item), source=SOURCE_ALERT, external_id=key, category=BOARD_CATEGORY,
                            priority="urgent" if urgent else "normal", status="open", created_at=now, updated_at=now)
        db.add(task)
        db.flush()
        db.add(ImperialTaskAudit(task_id=task.id, actor=actor, action="created", event_id=uuid4().hex))
        return "added"
    if needs_owner and task.status == DONE:
        task.status, task.updated_at, task.title = "open", now, alert_title(db, item)
        task.priority = "urgent" if urgent else "normal"
        db.add(ImperialTaskAudit(task_id=task.id, actor=actor, action="reopen", event_id=uuid4().hex))
        return "reopened"
    if not needs_owner and task.status != DONE:
        task.status, task.updated_at = DONE, now
        db.add(ImperialTaskAudit(task_id=task.id, actor=actor, action="complete", event_id=uuid4().hex))
        return "done"
    if needs_owner:
        title = alert_title(db, item)
        if title != task.title or (urgent and task.priority != "urgent"):
            task.title, task.priority = title, ("urgent" if urgent else task.priority)
    return ""


def sync_alert_ids(bind, item_ids) -> None:
    """Run in its own session after the dispatch change committed, so a problem here never
    undoes or blocks the customer message that caused it."""
    if not item_ids or not alerts_enabled():
        return
    db = Session(bind=bind)
    try:
        for item_id in sorted(item_ids):
            item = db.get(DispatchItem, item_id)
            if item is None:
                continue
            try:
                sync_alert(db, item)
                db.commit()
            except IntegrityError:  # another request added the same alert first
                db.rollback()
    except Exception:  # noqa: BLE001 - the board must never break message intake
        db.rollback()
        log.exception("shop_board_alert_sync_failed")
    finally:
        db.close()


def reconcile_alerts(db: Session) -> dict:
    """Catch-up pass: every item that needs the owner has an open to-do, and every open alert
    to-do whose item was handled moves to Completed."""
    counts = {"added": 0, "done": 0, "reopened": 0}
    if not alerts_enabled():
        return counts
    items = db.query(DispatchItem).filter(DispatchItem.status == NEEDS_OWNER).all()
    open_alerts = db.query(ImperialTask).filter(
        ImperialTask.category == BOARD_CATEGORY, ImperialTask.source == SOURCE_ALERT,
        ImperialTask.status.in_(OPEN_TODO)).all()
    ids = {i.id for i in items} | {i for i in (_alert_item_id(t.external_id) for t in open_alerts) if i}
    for item_id in sorted(ids):
        item = db.get(DispatchItem, item_id)
        if item is None:
            continue
        change = sync_alert(db, item)
        if change:
            counts[change] += 1
    db.commit()
    return counts


_last_reconcile = [0.0]


def maybe_reconcile(db: Session) -> None:
    if not alerts_enabled() or time.monotonic() - _last_reconcile[0] < RECONCILE_EVERY:
        return
    _last_reconcile[0] = time.monotonic()
    try:
        reconcile_alerts(db)
    except SQLAlchemyError:
        db.rollback()
        log.exception("shop_board_alert_reconcile_failed")


_PENDING = "shop_board_alert_ids"


def _collect(session, flush_context) -> None:
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, DispatchItem) and obj.id is not None:
            session.info.setdefault(_PENDING, set()).add(obj.id)


def _after_commit(session) -> None:
    ids = session.info.pop(_PENDING, None)
    if ids:
        sync_alert_ids(session.get_bind(), ids)


def _after_rollback(session) -> None:
    session.info.pop(_PENDING, None)


def register_alert_hooks() -> None:
    """Listen for dispatch item changes on every session. Cheap when nothing changed."""
    if not event.contains(Session, "after_flush", _collect):
        event.listen(Session, "after_flush", _collect)
        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_soft_rollback", lambda session, previous: _after_rollback(session))
