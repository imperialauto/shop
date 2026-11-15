"""Master Tasks as a phone-first board: Now, Waiting, Later, Done.

One card per customer conversation: Quo often adds an "UPDATE" task each time the same
customer writes again, so those stack into one card with the latest wording on top and the
older ones folded underneath. Each card carries what's needed to act without leaving the
page: who it is, the vehicle, a reply box (with the AI draft when there is one), call and
customer-file links. Read-only view model; changes go through imperial_tasks.change_task.
"""
from collections import OrderedDict
from datetime import datetime, time, timedelta, timezone

from app import quo_task_sync
from app.appointments import PHOENIX
from app.dispatch_model import DispatchItem, OPEN_STATUSES
from app.imperial_task_model import ImperialTask
from app.models import Customer

OPEN = ("open", "in_progress", "blocked")
TABS = OrderedDict([("now", "Now"), ("waiting", "Waiting"), ("later", "Later"), ("done", "Done")])


def _aware(value):
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(PHOENIX)


def _digits(phone):
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def _customer_name(customer):
    if not customer:
        return None
    name = f"{customer.first_name or ''} {customer.last_name or ''}".strip()
    return None if not name or name.lower().startswith("unknown") else name


def _context(db, conversation_id, phone, cache):
    """Customer, vehicle and the newest open queue item for a conversation or phone."""
    key = conversation_id or phone
    if key in cache:
        return cache[key]
    item = None
    query = db.query(DispatchItem)
    if conversation_id:
        item = query.filter(DispatchItem.quo_conversation_id == conversation_id).order_by(DispatchItem.updated_at.desc()).first()
    if item is None and phone:
        tail = _digits(phone)
        item = next((row for row in query.order_by(DispatchItem.updated_at.desc()).limit(400)
                     if _digits(row.customer_phone) == tail), None)
    phone = (item.customer_phone if item else None) or phone
    customer = db.get(Customer, item.customer_id) if item and item.customer_id else None
    if customer is None and phone:
        tail = _digits(phone)
        customer = next((c for c in db.query(Customer).filter(Customer.phone.isnot(None)).all()
                         if _digits(c.phone) == tail), None)
    open_item = None
    if item is not None:
        open_item = db.query(DispatchItem).filter(
            DispatchItem.quo_conversation_id == item.quo_conversation_id,
            DispatchItem.status.in_(OPEN_STATUSES),
        ).order_by(DispatchItem.updated_at.desc()).first()
    cache[key] = {
        "phone": phone,
        "customer_id": customer.id if customer else None,
        "name": _customer_name(customer) or (item.contact_name if item else None),
        "vehicle": item.vehicle_text if item else None,
        "item": open_item,
    }
    return cache[key]


def _bucket(task, end_of_today):
    if task.status == "done_verified":
        return "done"
    if task.status == "blocked":
        return "waiting"
    due = _aware(task.due_at)
    return "later" if due and due > end_of_today else "now"


def board(db, now: datetime | None = None) -> dict:
    now = (now or datetime.now(PHOENIX)).astimezone(PHOENIX)
    end_of_today = datetime.combine(now.date(), time(23, 59, 59), tzinfo=PHOENIX)
    week_ago = now - timedelta(days=7)
    rows = db.query(ImperialTask).filter(ImperialTask.status.in_(OPEN + ("done_verified",))).order_by(
        ImperialTask.id.desc()).all()
    groups: "OrderedDict[str, dict]" = OrderedDict()
    cache = {}
    for task in rows:
        bucket = _bucket(task, end_of_today)
        if bucket == "done" and (_aware(task.updated_at) or now) < week_ago:
            continue
        info = quo_task_sync.details(task) if task.source == quo_task_sync.SOURCE else {}
        conversation = info.get("conversationId")
        key = f"{bucket}:{conversation}" if conversation else f"{bucket}:task{task.id}"
        tag = quo_task_sync.label(task.title)
        entry = {"id": task.id, "title": tag["text"], "kind": tag["kind"], "update": tag["update"],
                 "created": _aware(task.created_at), "due": _aware(task.due_at)}
        if key not in groups:
            ctx = _context(db, conversation, info.get("phone"), cache) if (conversation or info.get("phone")) else {}
            groups[key] = {"key": key, "bucket": bucket, "tasks": [], "source": task.source,
                           "urgent": task.priority in ("urgent", "safety") or tag["urgent"], **ctx}
        group = groups[key]
        group["tasks"].append(entry)
        group["urgent"] = group["urgent"] or task.priority in ("urgent", "safety") or tag["urgent"]
    cards = {name: [] for name in TABS}
    for group in groups.values():
        group["tasks"].sort(key=lambda t: t["created"] or now, reverse=True)
        lead = group["tasks"][0]
        due = min((t["due"] for t in group["tasks"] if t["due"]), default=None)
        newest = lead["created"]
        group.update({
            "ids": ",".join(str(t["id"]) for t in group["tasks"]),
            "headline": lead["title"], "kind": lead["kind"], "older": group["tasks"][1:],
            "due": due,
            "due_label": (due.strftime("%a %b %d, %-I:%M %p") if due else ""),
            "overdue": bool(due and due < now and group["bucket"] != "done"),
            "age": _age(newest, now) if newest else "",
            "local_due": due.strftime("%Y-%m-%dT%H:%M") if due else "",
        })
        cards[group["bucket"]].append(group)
    for name in ("now", "waiting"):
        cards[name].sort(key=lambda g: (not g["urgent"], not g["overdue"], -(g["tasks"][0]["created"] or now).timestamp()))
    cards["later"].sort(key=lambda g: g["due"] or now)
    return {"tabs": [{"key": k, "label": v, "count": len(cards[k])} for k, v in TABS.items()], "cards": cards,
            "done_this_week": sum(len(g["tasks"]) for g in cards["done"])}


def _age(when, now):
    minutes = int((now - when).total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    if minutes < 24 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // (24 * 60)}d ago"


def tomorrow_morning(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(PHOENIX)).astimezone(PHOENIX)
    return datetime.combine(now.date() + timedelta(days=1), time(9, 0), tzinfo=PHOENIX)


def open_for_customer(db, conversation_ids, phone) -> list[dict]:
    """Open tasks about one customer (same Quo conversation or phone number)."""
    wanted = set(conversation_ids or [])
    tail = _digits(phone)
    out = []
    for task in db.query(ImperialTask).filter(ImperialTask.status.in_(OPEN)).order_by(ImperialTask.id.desc()).all():
        info = quo_task_sync.details(task) if task.source == quo_task_sync.SOURCE else {}
        if (info.get("conversationId") in wanted) or (tail and _digits(info.get("phone")) == tail):
            tag = quo_task_sync.label(task.title)
            out.append({"id": task.id, "kind": tag["kind"], "title": tag["text"],
                        "waiting": task.status == "blocked"})
    return out
