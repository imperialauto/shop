"""The one next step for a job, worked out from its own records (R38).

Nothing is stored here: the job's lines, approvals, assignment, invoice and
payments already say where it is, so the answer can never go stale. Each step
names who does it and is either one button (a POST to an existing route) or a
link to the spot on the page where it happens. Customer texts still go through
the owner's YES, and nothing here sends, charges or deletes anything.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.approval_links import conversation_key
from app.dispatch_model import OPEN_STATUSES, DispatchItem
from app.models import RepairOrder

STARTED = ("in_progress", "waiting_parts")


def _since(when) -> str:
    if not when:
        return ""
    when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - when).total_seconds() / 3600
    return f" for {int(hours)} hours" if hours < 48 else f" for {int(hours // 24)} days"


def for_job(db: Session, ro: RepairOrder, role: str) -> dict | None:
    """{"who", "title", "detail", "post"|"href", "button", "fields"} or None when the job is finished."""
    lines = ro.line_items
    invoice = next((i for i in ro.invoices if i.status != "void"), None)
    office = role != "tech"

    if ro.status == "delivered":
        return None
    if invoice is not None:
        paid = sum(p.amount for p in invoice.payments)
        due = round((invoice.total or 0) - paid, 2)
        if due > 0.009:
            return {"who": "Office", "title": f"Collect ${due:,.2f}", "detail": f"Invoice #{invoice.id} is {invoice.status}.",
                    "href": f"/invoices/{invoice.id}", "button": "Record payment"} if office else None
        return {"who": "Office", "title": "Paid in full. Hand over the keys",
                "detail": "Marking it picked up closes the job everywhere.",
                "post": f"/ro/{ro.id}/update-status", "fields": {"status": "delivered"}, "button": "Mark picked up"} if office else None
    if not lines:
        return {"who": "Tech or office", "title": "Add the repairs and prices",
                "detail": "Use the line items below, or the estimate tool.", "href": "#li-description", "button": "Add a line"}
    undecided = [li for li in lines if li.approval is None]
    pending = [li for li in lines if li.approval == "pending"]
    approved = [li for li in lines if li.approval == "approved"]
    if ro.status == "complete":
        if not any(li.billable for li in lines):
            return {"who": "Office", "title": "Nothing approved to bill",
                    "detail": "Every line was declined or is still waiting.", "href": "#li-description", "button": "Review lines"} if office else None
        return {"who": "Office", "title": "Work is done. Create the invoice",
                "detail": "Only approved lines are billed, parts taxed at the shop rate.",
                "post": f"/invoices/create/{ro.id}", "fields": {}, "button": "Create invoice"} if office else None
    if undecided and ro.status not in STARTED:
        return {"who": "Office", "title": f"Ask the customer to approve {len(undecided)} item{'s' if len(undecided) != 1 else ''}",
                "detail": "Queues the approval text for your YES. Nothing goes out on its own.",
                "post": f"/ro/{ro.id}/request-approval", "fields": {}, "button": "Ask for approval"} if office else None
    if pending and not approved:
        link = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                             DispatchItem.status.in_(OPEN_STATUSES + ("approved",))).first()
        waited = _since(min((li.created_at for li in pending if li.created_at), default=None))
        if link is not None and link.status in OPEN_STATUSES:
            return {"who": "Owner", "title": f"Approval text #{link.id} is waiting for your YES",
                    "detail": f"Reply YES {link.id} to the alert, or send it from the dashboard.", "href": "/dashboard#queue",
                    "button": "Open queue"}
        return {"who": "Customer", "title": f"Waiting on the customer's approval{waited}",
                "detail": "They have the link. Queue a reminder if it's been a while.",
                "post": f"/ro/{ro.id}/request-approval", "fields": {}, "button": "Queue a reminder"} if office else None
    if ro.status not in STARTED:
        if ro.assigned_tech_id is None and office:
            return {"who": "Office", "title": "Approved. Assign a tech", "detail": f"{len(approved)} approved item{'s' if len(approved) != 1 else ''}.",
                    "href": "#assign-tech", "button": "Choose tech"}
        return {"who": "Tech", "title": "Approved. Start the work", "detail": "Starting it shows on My day, Home and the board.",
                "post": f"/ro/{ro.id}/update-status", "fields": {"status": "in_progress"}, "button": "Start work"}
    if ro.status == "waiting_parts":
        return {"who": "Tech", "title": "Waiting on parts", "detail": "When they arrive, carry on.",
                "post": f"/ro/{ro.id}/update-status", "fields": {"status": "in_progress"}, "button": "Parts are here"}
    return {"who": "Tech", "title": "Finish the work", "detail": "Marking it complete tells the office to invoice.",
            "post": f"/ro/{ro.id}/update-status", "fields": {"status": "complete"}, "button": "Mark complete"}


STUCK_AFTER_HOURS = 48


def last_activity(ro: RepairOrder):
    """The newest thing that happened on the job: status change, line, decision, invoice or payment."""
    stamps = [ro.updated_at, ro.created_at, ro.signed_at]
    for li in ro.line_items:
        stamps += [li.created_at, li.decided_at]
    for inv in ro.invoices:
        stamps.append(inv.created_at)
        stamps += [p.paid_at for p in inv.payments]
    stamps = [t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in stamps if t]
    return max(stamps) if stamps else None


def stuck_jobs(db: Session, now: datetime, limit: int = 5) -> list[tuple[RepairOrder, dict, float]]:
    """Open jobs whose next step hasn't moved in two days, oldest first. Read-only."""
    found = []
    for ro in db.query(RepairOrder).filter(RepairOrder.status != "delivered").all():
        step = for_job(db, ro, "owner")
        last = last_activity(ro)
        if step is None or last is None:
            continue
        hours = (now - last).total_seconds() / 3600
        if hours >= STUCK_AFTER_HOURS:
            found.append((ro, step, hours))
    found.sort(key=lambda row: -row[2])
    return found[:limit]


def stuck_text(rows) -> str:
    lines = ["Stuck jobs (no movement in 2+ days):"]
    for ro, step, hours in rows:
        v = ro.vehicle
        car = " ".join(str(x) for x in (v.make, v.model) if x) if v else ""
        who = (ro.customer.first_name or "").strip() if ro.customer else ""
        lines.append(f"#{ro.id} {who}{' ' + car if car else ''}: {step['title']} ({int(hours // 24)}d)")
    lines.append("Each job's page has the one-tap next step.")
    return "\n".join(lines)
