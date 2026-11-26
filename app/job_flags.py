"""Things on a job that don't add up, in plain words for the owner.

The owner asked: "when something doesn't make sense, it needs to be flagged." Each flag is
{"code", "message", "severity"}: severity "warn" for something that looks wrong and should be
fixed, "info" for something worth a glance. The message is one sentence that says what to do.

Read only: no database writes, no texts, no status changes. A job closed as not going ahead
("cancelled") has nothing to flag. A picked-up job ("delivered") is still checked for lines
that contradict it, but not for being old or duplicated.
"""
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import RepairOrder
from app.ro_status_policy import CLOSED_STATUSES, is_open

WAITING_DAYS = 14   # pending lines with no answer for this long
STALE_DAYS = 60     # open job with no activity for this long

STATUS_WORDS = {
    "intake": "Intake",
    "diagnosing": "Diagnosing",
    "waiting_parts": "Waiting on parts",
    "in_progress": "In progress",
    "waiting_approval": "Waiting on approval",
    "approval_received": "Approval received",
    "complete": "Complete",
    "delivered": "Picked up",
    "cancelled": "Closed, not going ahead",
}

# Words that say what's being done, where, or how much, not which part. Two lines sharing only
# these are not about the same repair.
_GENERIC = frozenset("""
a an and the of for to with on in at by or new used oem aftermarket reman remanufactured genuine
replace replaced replacement install installed installation remove removal labor labour part parts
kit set assembly assy service repair diag diagnose diagnosis diagnostic inspect inspection check
test r&r rr front rear left right upper lower inner outer driver passenger side both pair each
qty quantity misc supplies shop fee fees hour hours hr hrs
""".split())


def _utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z&]+", (text or "").lower()):
        if len(w) < 3 or w in _GENERIC:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # pads/pad, gaskets/gasket
        if w not in _GENERIC:
            out.add(w)
    return out


def _same_repair(a: str, b: str) -> bool:
    """Generous match: the shared real words are more than half of the shorter description's real
    words ("Oil pan gasket" and "Replace oil pan gasket" match; "Front brake pads" and "Rear brake
    rotors" don't)."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    shared = wa & wb
    return len(shared) * 2 > min(len(wa), len(wb))


def _flag(code, message, severity="warn"):
    return {"code": code, "message": message, "severity": severity}


def _money(x):
    return f"${x:,.2f}"


def _customer_name(c):
    if c is None:
        return "this customer"
    if c.customer_type == "fleet" and c.fleet_name:
        return c.fleet_name
    return f"{c.first_name or ''} {c.last_name or ''}".strip() or "this customer"


def _phone_key(phone):
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else None


def _same_vehicle(a, b) -> bool:
    if a is None or b is None:
        return False
    if a.id == b.id:
        return True
    if a.vin and b.vin and a.vin.strip().upper() == b.vin.strip().upper():
        return True
    ymm = lambda v: (v.year, (v.make or "").strip().lower(), (v.model or "").strip().lower())
    return bool(a.make and a.model) and ymm(a) == ymm(b)


def _same_customer(a, b) -> bool:
    if a is None or b is None:
        return False
    if a.id == b.id:
        return True
    pa, pb = _phone_key(a.phone), _phone_key(b.phone)
    return bool(pa) and pa == pb


def _open_jobs(db: Session):
    return (db.query(RepairOrder)
            .filter((RepairOrder.status.is_(None)) | (RepairOrder.status.notin_(CLOSED_STATUSES)))
            .order_by(RepairOrder.id)
            .all())


def _mismatched_pairs(lines):
    flags = []
    parts = [li for li in lines if li.item_type == "part"]
    labor = [li for li in lines if li.item_type in ("labor", "sublet")]
    seen = set()
    for p in parts:
        for l in labor:
            if {p.approval, l.approval} != {"approved", "declined"} or (p.id, l.id) in seen:
                continue
            if not _same_repair(p.description, l.description):
                continue
            seen.add((p.id, l.id))
            if p.approval == "approved":
                msg = (f'The part "{p.description}" is approved but its labor "{l.description}" was declined; '
                       f"check with the customer and fix whichever line is wrong.")
            else:
                msg = (f'The labor "{l.description}" is approved but its part "{p.description}" was declined; '
                       f"check with the customer and fix whichever line is wrong.")
            flags.append(_flag("part_labor_mismatch", msg))
    return flags


def _status_flags(ro, lines):
    flags = []
    status = ro.status or "intake"
    word = STATUS_WORDS.get(status, status)
    pending = [li for li in lines if li.approval == "pending"]
    approved = [li for li in lines if li.approval == "approved"]

    if status == "waiting_approval" and not pending:
        flags.append(_flag("status_waiting_no_pending",
                           "It says Waiting on approval but nothing is waiting on the customer; "
                           "move it to the status it's really at."))
    if status in ("in_progress", "waiting_parts", "complete") and not approved:
        flags.append(_flag("status_no_approved",
                           f"It says {word} but no line is approved; record the customer's yes "
                           f"or move the job back."))
    if status == "approval_received" and pending:
        flags.append(_flag("status_approved_but_pending",
                           f"It says Approval received but {len(pending)} line(s) still wait on the "
                           f"customer; record their answer on those lines."))
    if status == "delivered" and pending:
        flags.append(_flag("delivered_with_pending",
                           f"It was picked up but {len(pending)} line(s) still wait on the customer; "
                           f"mark them approved or declined so the bill is right."))
    if status in ("complete", "delivered"):
        undone = [li for li in approved if not li.completed_at]
        if undone:
            flags.append(_flag("approved_not_done",
                               f"It says {word} but {len(undone)} approved line(s) aren't marked done; "
                               f"tick them done or take them off.", "info"))
    return flags


def _waiting_flag(lines, now):
    pending = [li for li in lines if li.approval == "pending"]
    if not pending:
        return None
    asked = max((_utc(li.changed_at) or _utc(li.created_at) for li in pending
                 if (li.changed_at or li.created_at)), default=None)
    if asked is None:
        return None
    days = (now - asked).days
    if days < WAITING_DAYS:
        return None
    return _flag("waiting_too_long",
                 f"The customer hasn't answered in {days} days; send one last nudge or close it out "
                 f"as not going ahead.")


def _duplicate_flag(db, ro, open_jobs):
    if open_jobs is None:
        open_jobs = _open_jobs(db)
    others = [o for o in open_jobs
              if o.id != ro.id and _same_customer(o.customer, ro.customer) and _same_vehicle(o.vehicle, ro.vehicle)]
    if not others:
        return None
    nums = ", ".join(f"RO #{o.id}" for o in others)
    return _flag("duplicate_open_job",
                 f"{_customer_name(ro.customer)} has another open job on this vehicle ({nums}); "
                 f"if it's the same visit, close one out.")


def _stale_flag(ro, now):
    last = _utc(ro.updated_at) or _utc(ro.created_at)
    if last is None:
        return None
    days = (now - last).days
    if days < STALE_DAYS:
        return None
    return _flag("stale_job",
                 f"Nothing has happened on this job in {days} days; close it out if the vehicle is gone, "
                 f"or update where it stands.")


def _invoice_flags(ro, lines):
    flags = []
    billable = [li for li in lines if li.billable]
    subtotal = round(sum(li.total for li in billable), 2)
    parts = sum(li.total for li in billable if li.item_type == "part")
    for inv in ro.invoices:
        if inv.status == "void" or inv.total is None:
            continue
        expected = round(subtotal + round(parts * (inv.tax_rate or 0), 2), 2)
        if abs(round(inv.total, 2) - expected) > 0.01:
            flags.append(_flag("invoice_mismatch",
                               f"Invoice #{inv.id} is for {_money(inv.total)} but the approved lines come to "
                               f"{_money(expected)} with tax; void and remake the invoice or fix the lines."))
    return flags


def for_job(db: Session, ro: RepairOrder, *, now=None, _open_jobs_cache=None) -> list[dict]:
    """Every flag on one job, warnings first. Empty for a clean job or one closed as not going ahead."""
    if ro is None or ro.status == "cancelled":
        return []
    now = _utc(now) or _now()
    lines = [li for li in ro.line_items if li.item_type != "discount"]
    all_lines = list(ro.line_items)

    flags = _mismatched_pairs(lines) + _status_flags(ro, lines)
    if is_open(ro):
        waiting = _waiting_flag(lines, now)
        if waiting:
            flags.append(waiting)
        dup = _duplicate_flag(db, ro, _open_jobs_cache)
        if dup:
            flags.append(dup)
        if not waiting:  # an old unanswered estimate already says the same thing
            stale = _stale_flag(ro, now)
            if stale:
                flags.append(stale)
    # Invoices are totalled from every billable line, discounts included.
    flags += _invoice_flags(ro, all_lines)
    flags.sort(key=lambda f: f["severity"] != "warn")
    return flags


def open_jobs_with_flags(db: Session, limit: int = 20, *, now=None) -> list[tuple]:
    """Open jobs that have at least one flag, as (ro, flags): most warnings first, then oldest job."""
    jobs = _open_jobs(db)
    out = []
    for ro in jobs:
        flags = for_job(db, ro, now=now, _open_jobs_cache=jobs)
        if flags:
            out.append((ro, flags))
    out.sort(key=lambda pair: (-sum(f["severity"] == "warn" for f in pair[1]), pair[0].id))
    return out[:limit]
