"""A customer's plain "yes" text to a quote moves their job to Approved by itself.

Off unless TEXT_YES_APPROVES=true. It only acts when every check below holds, and texts nobody:
- the inbound text is a short, plain yes with no question and no "no", "wait", "but" and the like;
- the shop's last text to that customer, within the past 7 days, was a quote: it names a price, or is the
  estimate-link text or a quote follow-up. Deposit, pickup, diagnostic-credit, oil and Snap texts never count,
  even with a $ amount, because a "yes" or "ok" to those isn't a yes to new work;
- the customer has exactly one open job with lines still waiting on a decision.
Then those lines are marked approved by text, the job moves to approval_received, any queued
approval-link text is closed, and the owner gets one task to look it over. When the yes fits but
the job is unclear (two open jobs), nothing changes and the owner gets a task to pick.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import approval_links
from app.dispatch_model import DispatchAudit
from app.imperial_task_model import PERSONAL_CATEGORY, ImperialTask, ImperialTaskAudit
from app.models import Communication, Customer, RepairOrder, User

log = logging.getLogger(__name__)

TASK_SOURCE = "text_yes"
QUOTE_WINDOW = timedelta(days=7)
CLOSED_STATUSES = {"complete", "delivered"}
YES_PHRASES = (
    "yes", "yes please", "yes sir", "yes maam", "yeah", "yea", "yep", "yup", "ok", "okay", "ok go ahead",
    "okay go ahead", "go ahead", "go for it", "do it", "lets do it", "lets do that", "sounds good",
    "sounds good go ahead", "approved", "approve", "i approve", "deal", "bet", "lets go", "please do",
)
BLOCKERS = re.compile(
    r"\b(no|nah|not|dont|cant|wont|wait|hold|later|but|cancel|stop|how|what|why|when|which|maybe|"
    r"think|cheaper|discount|lower|instead|only|except|first|tomorrow|next)\b")
PRICE = re.compile(r"\$\s?\d")
# The shop's own texts that are not quotes, by wording only they use (see deposit_requests, pickup_texts,
# diag_credit, oil_reminders and snap_link). A reply to one of these never approves work.
NOT_A_QUOTE = re.compile(
    r"thanks for approving|deposit, half of the|ready for pickup|balance due|you're all paid up|storage of \$|"
    r"from the diagnostic on your|since the last oil change|link to apply for snap finance", re.I)
# The shop's own texts that are quotes even without a $ amount: the estimate link and the quote follow-up.
QUOTE_WITHOUT_PRICE = re.compile(r"your estimate for the .* is ready|we quoted for your", re.I)


def is_quote(body: str) -> bool:
    text = body or ""
    if NOT_A_QUOTE.search(text):
        return False
    return bool(PRICE.search(text) or QUOTE_WITHOUT_PRICE.search(text))


def enabled() -> bool:
    return os.getenv("TEXT_YES_APPROVES", "").strip().lower() in ("1", "true", "yes", "on")


def is_plain_yes(body: str) -> bool:
    raw = (body or "").strip()
    if not raw or len(raw) > 60 or "?" in raw:
        return False
    words = re.sub(r"[^a-z ]+", " ", raw.lower().replace("'", "")).split()
    if not words:
        return False
    text = " ".join(words)
    if BLOCKERS.search(text):
        return False
    return any(text == p or text.startswith(p + " ") for p in YES_PHRASES)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _last_quote_before(db: Session, customer_id: int, inbound: Communication) -> Communication | None:
    when = _naive_utc(inbound.created_at) or datetime.utcnow()
    last = (db.query(Communication)
            .filter(Communication.customer_id == customer_id, Communication.direction == "outbound",
                    Communication.id != inbound.id, Communication.created_at <= when)
            .order_by(Communication.created_at.desc(), Communication.id.desc()).first())
    if not last or not is_quote(last.body):
        return None
    if when - (_naive_utc(last.created_at) or when) > QUOTE_WINDOW:
        return None
    return last


def _waiting_jobs(db: Session, customer_id: int) -> list[tuple[RepairOrder, list]]:
    jobs = []
    for ro in (db.query(RepairOrder).filter(RepairOrder.customer_id == customer_id)
               .order_by(RepairOrder.created_at.desc()).all()):
        if (ro.status or "") in CLOSED_STATUSES or any(inv.status != "void" for inv in ro.invoices):
            continue
        lines = [li for li in ro.line_items if li.approval in (None, "pending")]
        if lines and (any(li.approval == "pending" for li in ro.line_items) or ro.status in ("intake", "waiting_approval")):
            jobs.append((ro, lines))
    return jobs


def _owner_task(db: Session, key: str, title: str, details: str) -> None:
    from app.routes.imperial_tasks import enabled as tasks_enabled
    if not tasks_enabled():
        return
    if db.query(ImperialTask).filter_by(source=TASK_SOURCE, external_id=key).first():
        return
    owner = db.query(User).filter(User.role == "owner").order_by(User.id).first()
    now = datetime.now(timezone.utc)
    task = ImperialTask(source=TASK_SOURCE, external_id=key[:128], category=PERSONAL_CATEGORY, status="open",
                        priority="high", title=title[:200], details=details, due_at=now,
                        assigned_user_id=owner.id if owner else None)
    db.add(task)
    db.flush()
    db.add(ImperialTaskAudit(task_id=task.id, actor="customer_text", action="created", event_id=f"{TASK_SOURCE}:{key}"[:128]))


def handle_inbound(db: Session, customer: Customer, comm: Communication) -> dict | None:
    """Called after an inbound text is logged. Returns what it did, or None when it did nothing."""
    if not enabled() or comm.direction != "inbound" or not is_plain_yes(comm.body):
        return None
    if _last_quote_before(db, customer.id, comm) is None:
        return None
    name = (customer.first_name or "A customer").strip()
    jobs = _waiting_jobs(db, customer.id)
    if not jobs:
        return None
    key = f"msg:{comm.external_id or comm.id}"
    if len(jobs) > 1:
        numbers = ", ".join(f"#{ro.id}" for ro, _ in jobs)
        _owner_task(db, key, f"{name} texted yes: pick which job ({numbers})",
                    f'{name} replied "{(comm.body or "").strip()[:60]}" to your quote, but has more than one open '
                    f"job ({numbers}). Nothing was changed. Open the right job and approve its lines.")
        db.add(DispatchAudit(actor="customer_text", action="text_yes_unclear", detail=f"customer {customer.id}"))
        db.commit()
        return {"action": "unclear", "jobs": [ro.id for ro, _ in jobs]}
    ro, lines = jobs[0]
    now = datetime.now(timezone.utc)
    for li in lines:
        li.approval, li.decided_at = "approved", now
        li.approval_source, li.approval_recorded_by = "text", "customer text"
    if not any(li.approval == "pending" for li in ro.line_items):
        if ro.status in ("intake", "waiting_approval"):
            ro.status = "approval_received"
        approval_links.close_for(db, ro, actor="customer_text", action="approved_by_text")
    db.add(DispatchAudit(actor="customer_text", action="text_yes_approved",
                         detail=f"job {ro.id}, {len(lines)} lines"))
    _owner_task(db, key, f"{name} texted yes: job #{ro.id} moved to Approved",
                f'{name} replied "{(comm.body or "").strip()[:60]}" to your quote, so {len(lines)} line(s) on job '
                f"#{ro.id} were marked approved by text. Take the deposit and book the work. If that yes wasn't "
                "for this job, open it and change the lines back.")
    db.commit()
    return {"action": "approved", "job": ro.id, "lines": len(lines)}
