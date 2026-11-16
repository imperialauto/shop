"""Master Tasks groups work by who needs to act and when.

One card per customer conversation: Quo often adds an "UPDATE" task each time the same
customer writes again, so those stack into one card with the latest wording on top and the
older ones folded underneath. Each card carries what's needed to act without leaving the
page: who it is, the vehicle, a reply box (with the AI draft when there is one), call and
customer-file links. Read-only view model; changes go through imperial_tasks.change_task.
"""
from collections import OrderedDict
from datetime import datetime, time, timedelta, timezone

from app import business_settings, quo_task_sync
from app.appointments import PHOENIX
from app.dispatch_model import DispatchItem, OPEN_STATUSES
from app.imperial_task_model import BACKLOG_CATEGORY, PERSONAL_CATEGORY, PROBLEM_CATEGORY, ImperialTask, ImperialTaskAudit
from app.models import Communication, Customer, RepairOrder

OPEN = ("open", "in_progress", "blocked")
TABS = OrderedDict([("now", "Now"), ("waiting", "Waiting"), ("followup", "Follow-up"), ("later", "Later"), ("done", "Done")])
SORTS = {"priority": "Priority", "newest": "Newest activity", "oldest": "Oldest activity", "due": "Due first"}
PERSONAL_WINDOW = timedelta(hours=3)
CALLBACK_SOURCE = "dispatch_callback"


def _aware(value):
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(PHOENIX)


def _digits(phone):
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def _customer_name(customer):
    if not customer:
        return None
    name = f"{customer.first_name or ''} {customer.last_name or ''}".strip().rstrip("-").strip()
    return None if not name or name.lower().startswith("unknown") else name


def _context(db, conversation_id, phone, cache, activity_id=None):
    """Customer, vehicle and the newest open queue item for a phone or conversation.

    The phone written in the task title wins: some Quo tasks hang off the owner's own
    thread with Imperial, so their conversation is not the customer's.
    """
    key = _digits(phone) or (conversation_id, activity_id)
    if key in cache:
        return cache[key]
    item = None
    query = db.query(DispatchItem)
    if phone:
        tail = _digits(phone)
        item = next((row for row in query.order_by(DispatchItem.updated_at.desc()).limit(600)
                     if _digits(row.customer_phone) == tail), None)
    if item is None and conversation_id and not phone:
        item = query.filter(DispatchItem.quo_conversation_id == conversation_id).order_by(DispatchItem.updated_at.desc()).first()
        if item is not None and _is_owner(item.customer_phone):
            item = None
    phone = (item.customer_phone if item else None) or phone
    customer = db.get(Customer, item.customer_id) if item and item.customer_id else None
    if customer is None and not phone and activity_id:
        customer = db.query(Customer).join(Communication, Communication.customer_id == Customer.id).filter(
            Communication.external_id == activity_id, Communication.channel == "sms",
        ).first()
        if customer and not _is_owner(customer.phone):
            phone = customer.phone
        else:
            customer = None
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
    latest_sms = None
    if customer:
        latest_sms = db.query(Communication).filter(
            Communication.customer_id == customer.id, Communication.channel == "sms",
            Communication.direction.in_(("inbound", "outbound")),
        ).order_by(Communication.created_at.desc(), Communication.id.desc()).first()
    cache[key] = {
        "phone": phone,
        "customer_id": customer.id if customer else None,
        "name": _customer_name(customer) or _clean_label(item.contact_name if item else None),
        "vehicle": item.vehicle_text if item else None,
        "item": open_item,
        "latest_sms": latest_sms,
    }
    return cache[key]


def _is_owner(phone):
    from app.config import OWNER_PHONE
    return bool(phone) and _digits(phone) == _digits(OWNER_PHONE)


def _clean_label(name):
    """Quo contact labels: "Amaya -" -> "Amaya"; the owner's own label is not a customer."""
    name = (name or "").strip().rstrip("-").strip()
    return None if not name or "jaelan" in name.lower() else name


def _bucket(task, end_of_today):
    if task.status == "done_verified":
        return "done"
    if task.status == "blocked":
        return "waiting"
    due = _aware(task.due_at)
    return "later" if due and due > end_of_today else "now"


def personal_plan(db, user_id, now):
    """A short owner agenda backed by the existing task records."""
    rows = db.query(ImperialTask).filter(
        ImperialTask.status.in_(OPEN),
        ImperialTask.category.in_((PERSONAL_CATEGORY, BACKLOG_CATEGORY)),
        (ImperialTask.assigned_user_id == user_id) | ImperialTask.assigned_user_id.is_(None),
    ).all()
    plan = {"next": [], "overdue": [], "later": [], "backlog": []}
    for task in rows:
        due = _aware(task.due_at)
        if task.category == BACKLOG_CATEGORY or task.status == "blocked":
            group = "backlog"
        elif due and due.date() < now.date():
            group = "overdue"
        elif due and due > now + PERSONAL_WINDOW:
            group = "later"
        else:
            group = "next"
        info = quo_task_sync.details(task) if task.source in (CALLBACK_SOURCE, quo_task_sync.SOURCE) else {}
        plan[group].append({
            "id": task.id, "title": task.title, "due": due,
            "note": info.get("note") if info else task.details,
            "phone": info.get("phone") or quo_task_sync.phone_in(task.title),
            "customer_id": info.get("customerId"),
            "priority": task.priority,
        })
    for items in plan.values():
        items.sort(key=lambda item: (item["priority"] not in ("safety", "urgent"), item["due"] or now, item["id"]))
    return plan


def _conversation_bucket(task, bucket, context, manual_now, now, followup_days):
    sms = context.get("latest_sms")
    if not sms or bucket in ("done", "later"):
        return bucket, None, False
    at = _aware(sms.created_at)
    if at is None or bucket == "now" and manual_now and at <= manual_now:
        return bucket, at, False
    if sms.direction == "inbound":
        updated = _aware(task.updated_at)
        if bucket == "waiting" and (updated is None or at > updated):
            return "now", at, False
        return bucket, at, False
    open_item = context.get("item")
    if task.priority == "safety" or open_item and open_item.urgency == "safety":
        return bucket, at, False
    if open_item:
        updated = _aware(open_item.updated_at)
        from_call = bool(open_item.thread_message_id) or open_item.last_message_id.startswith(("call:", "summary:"))
        if updated and updated > at and from_call:
            return bucket, updated, False
    return ("followup" if now - at >= timedelta(days=followup_days) else "waiting"), at, True


def board(db, now: datetime | None = None, *, linked: bool | None = None, sort: str = "priority") -> dict:
    now = (now or datetime.now(PHOENIX)).astimezone(PHOENIX)
    end_of_today = datetime.combine(now.date(), time(23, 59, 59), tzinfo=PHOENIX)
    week_ago = now - timedelta(days=7)
    followup_days = business_settings.task_followup_days()
    rows = db.query(ImperialTask).filter(ImperialTask.status.in_(OPEN + ("done_verified",)),
                                         ImperialTask.category.notin_((PROBLEM_CATEGORY, PERSONAL_CATEGORY, BACKLOG_CATEGORY))).order_by(
        ImperialTask.id.desc()).all()
    groups: "OrderedDict[str, dict]" = OrderedDict()
    cache = {}
    row_ids = {task.id for task in rows}
    pins = {task_id: _aware(created_at) for task_id, created_at in db.query(
        ImperialTaskAudit.task_id, ImperialTaskAudit.created_at,
    ).filter(ImperialTaskAudit.action == "now").order_by(ImperialTaskAudit.id) if task_id in row_ids}
    orders = {}
    for task in rows:
        bucket = _bucket(task, end_of_today)
        if bucket == "done" and (_aware(task.updated_at) or now) < week_ago:
            continue
        info = quo_task_sync.details(task) if task.source == quo_task_sync.SOURCE else {}
        conversation = info.get("conversationId")
        who = _digits(info.get("phone")) or conversation or info.get("activityId")
        job_id = int(task.project_key[3:]) if task.project_key and task.project_key.startswith("ro-") and task.project_key[3:].isdigit() else None
        if job_id and job_id not in orders:
            orders[job_id] = db.get(RepairOrder, job_id)
        order = orders.get(job_id)
        if order and order.status == "delivered" and bucket != "done":
            order = None
        if linked is not None and linked != bool(order):
            continue
        ctx = _context(db, conversation, info.get("phone"), cache, info.get("activityId")) if who else {}
        who = _digits(ctx.get("phone")) or who
        activity, waiting = None, False
        if who and not order:
            bucket, activity, waiting = _conversation_bucket(task, bucket, ctx, pins.get(task.id), now, followup_days)
        key = f"{bucket}:{who}" if who and not order else f"{bucket}:task{task.id}"
        tag = quo_task_sync.label(task.title)
        entry = {"id": task.id, "title": tag["text"], "kind": tag["kind"], "update": tag["update"],
                 "created": _aware(task.created_at), "due": _aware(task.due_at)}
        if key not in groups:
            groups[key] = {"key": key, "bucket": bucket, "tasks": [], "source": task.source,
                           "urgent": False,
                           "repair_order_id": order.id if order else None,
                           "note": task.details if task.source == "dashboard" else None,
                           "activity": activity, "waiting_on_customer": waiting, **ctx}
        group = groups[key]
        group["tasks"].append(entry)
        group["urgent"] = group["urgent"] or task.priority in ("urgent", "safety") or tag["urgent"]
    cards = {name: [] for name in TABS}
    for group in groups.values():
        group["tasks"].sort(key=lambda t: t["created"] or now, reverse=True)
        lead = group["tasks"][0]
        due = min((t["due"] for t in group["tasks"] if t["due"]), default=None)
        newest = lead["created"]
        activity = group["activity"] or newest or now
        group.update({
            "ids": ",".join(str(t["id"]) for t in group["tasks"]),
            "headline": lead["title"], "kind": lead["kind"], "older": group["tasks"][1:],
            "due": due,
            "due_label": (due.strftime("%a %b %d, %-I:%M %p") if due else ""),
            "urgent": group["urgent"] and not group["waiting_on_customer"],
            "overdue": bool(due and due < now and group["bucket"] == "now"),
            "age": _age(activity, now),
            "activity": activity,
            "local_due": due.strftime("%Y-%m-%dT%H:%M") if due else "",
        })
        cards[group["bucket"]].append(group)
    for name, items in cards.items():
        if sort == "oldest" or (sort == "priority" and name == "followup"):
            items.sort(key=lambda g: g["activity"])
        elif sort == "due" or (sort == "priority" and name == "later"):
            items.sort(key=lambda g: (g["due"] is None, g["due"] or now))
        elif sort == "newest" or name == "done":
            items.sort(key=lambda g: g["activity"], reverse=True)
        else:
            items.sort(key=lambda g: (not g["urgent"], not g["overdue"], -g["activity"].timestamp()))
    return {"tabs": [{"key": k, "label": v, "count": len(cards[k])} for k, v in TABS.items()], "cards": cards,
            "followup_days": followup_days, "done_this_week": sum(len(g["tasks"]) for g in cards["done"])}


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
    for task in db.query(ImperialTask).filter(ImperialTask.status.in_(OPEN), ImperialTask.category.notin_(
            (PROBLEM_CATEGORY, PERSONAL_CATEGORY, BACKLOG_CATEGORY))).order_by(ImperialTask.id.desc()).all():
        info = quo_task_sync.details(task) if task.source == quo_task_sync.SOURCE else {}
        if (info.get("conversationId") in wanted) or (tail and _digits(info.get("phone")) == tail):
            tag = quo_task_sync.label(task.title)
            out.append({"id": task.id, "kind": tag["kind"], "title": tag["text"],
                        "waiting": task.status == "blocked"})
    return out
