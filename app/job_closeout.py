"""Owner close-out: mark a job done (and picked up) in one step, with no text to the customer.

For jobs where the customer said yes in person or on a call and the work is already finished, so
the normal path (send for approval, approve, start, complete, picked up) doesn't fit. The owner
ticks the lines the customer agreed to; those are recorded as approved in person under the
owner's name. Lines left unticked that were never agreed are marked declined, so they're not
billed. Ticked lines are marked done. The job moves to Complete, or straight to Picked up.

Nothing is texted: the ready-for-pickup text and the review request that the status buttons
start are skipped on purpose, any queued approval text is cancelled, and the website request is
closed. No invoice is made here; make one from the job page if the customer owes anything.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.dispatch_model import DispatchAudit
from app.models import RepairOrder

SOURCES = ("in_person", "phone")


class CloseOutError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def problem(ro: RepairOrder) -> CloseOutError | None:
    if ro.status == "delivered":
        return CloseOutError(409, "This job is already picked up.")
    if ro.status == "cancelled":
        return CloseOutError(409, "This job was closed as not going ahead. Reopen it first.")
    return None


def close_out(db: Session, ro: RepairOrder, actor: str, approved_ids, picked_up: bool,
              source: str = "in_person", note: str = "") -> dict:
    """Caller commits. Returns what changed. Raises CloseOutError."""
    err = problem(ro)
    if err:
        raise err
    if source not in SOURCES:
        raise CloseOutError(422, "Choose in person or by phone.")
    wanted = {int(i) for i in approved_ids or []}
    lines = {li.id: li for li in ro.line_items}
    unknown = wanted - set(lines)
    if unknown:
        raise CloseOutError(422, "One of the ticked lines isn't on this job any more. Reload and try again.")
    work = [li for li in ro.line_items if li.item_type != "discount"]
    if not any(li.approval == "approved" for li in work) and not wanted:
        raise CloseOutError(422, "Tick at least one item the customer agreed to.")
    invoiced = any(inv.status != "void" for inv in ro.invoices)
    now = datetime.now(timezone.utc)
    who = (actor or "owner")[:64]
    approved, declined, done = [], [], []
    for li in work:
        if li.id in wanted and li.approval != "approved":
            if invoiced:
                raise CloseOutError(409, "This job has an invoice that doesn't include every ticked line. "
                                         "Void it first, or leave those lines unticked.")
            li.approval, li.decided_at = "approved", now
            li.approval_source, li.approval_recorded_by = source, who
            approved.append(li)
        elif li.id not in wanted and li.approval in (None, "pending"):
            li.approval, li.decided_at = "declined", now
            li.approval_source, li.approval_recorded_by = "shop", who
            declined.append(li)
        if li.approval == "approved" and not li.completed_at and hasattr(li, "completed_at"):
            li.completed_at = now
            if hasattr(li, "completed_by"):
                li.completed_by = who
            done.append(li)
    for li in ro.line_items:
        if li.item_type == "discount" and li.approval in (None, "pending"):
            li.approval, li.decided_at, li.approval_source = "approved", now, "shop"
    ro.removed_since_approval = None
    before = ro.status
    ro.status = "delivered" if picked_up else "complete"
    from app import approval_links, website_alerts
    approval_links.close_for(db, ro, actor=who, action="job_closed_out")
    website_alerts.close_for_job(db, ro, who, "job closed out by owner")
    detail = (f"Job #{ro.id} {before} -> {ro.status}: {len(approved)} approved {source.replace('_', ' ')}, "
              f"{len(declined)} not agreed. No text sent." + (f" {note.strip()}" if note and note.strip() else ""))
    db.add(DispatchAudit(item_id=None, actor=who, action="job_closed_out", detail=detail[:255]))
    return {"status": ro.status, "previous": before, "approved": [li.id for li in approved],
            "declined": [li.id for li in declined], "marked_done": [li.id for li in done]}
