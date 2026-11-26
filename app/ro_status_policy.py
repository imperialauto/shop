"""Server-side guardrails for staff repair-order status changes.

Customer approvals are data, not a dropdown state: staff cannot substitute a
status change for a signed/recorded line decision. The customer-signature and
estimate-approval routes remain independent and keep their existing semantics.

This module has no side effects, database writes, or outbound messaging.
"""
from app.models import RepairOrder


# A job in one of these is finished with: picked up, or closed as not going ahead (a duplicate or a
# quote the customer passed on). Everything else is an open job.
CLOSED_STATUSES = ("delivered", "cancelled")


def is_open(ro) -> bool:
    return (getattr(ro, "status", None) or "intake") not in CLOSED_STATUSES


FINISH_FROM = frozenset(("diagnosing", "approval_received", "in_progress", "waiting_parts", "complete"))


def status_error(ro: RepairOrder, target: str, role: str) -> tuple[int, str] | None:
    """Return (HTTP code, user-facing explanation) or None if permitted."""
    if target == ro.status:
        return None

    if role == "tech" and target in ("waiting_approval", "approval_received", "delivered"):
        return 403, "Customer approvals and vehicle release are handled by the office."

    if target == "cancelled":
        if role != "owner":
            return 403, "Only the owner can close a job as not going ahead."
        if ro.status == "delivered":
            return 409, "This job was picked up. It can't be closed as not going ahead."
        if any(inv.status != "void" for inv in getattr(ro, "invoices", []) or []):
            return 409, "This job has an invoice. Void it first if the job really isn't going ahead."
        return None
    if ro.status == "cancelled" and role != "owner":
        return 403, "Only the owner can reopen a job that was closed as not going ahead."

    lines = ro.line_items
    approved = any(line.approval == "approved" for line in lines)
    unresolved = any(line.approval in (None, "pending") for line in lines)

    if target == "waiting_approval" and not any(line.approval == "pending" for line in lines):
        return 409, "Use Ask for approval to queue the estimate and record pending lines first."

    if target == "approval_received" and (
        not lines or unresolved or not any(line.approval in ("approved", "declined") for line in lines)
    ):
        return 409, "Record the customer's decision on each quoted line before marking approval received."

    # Diagnosis can begin before a repair estimate exists, but a repair or
    # parts-hold status must have at least one approved item on file.
    if target in ("in_progress", "waiting_parts") and not approved:
        return 409, "Approve at least one quoted repair before starting work or waiting for repair parts."

    if target == "complete" and unresolved and not approved:
        return 409, "No quoted work has been approved. Resolve the estimate before completing repairs."

    if target == "complete" and ro.status not in FINISH_FROM:
        return 409, "Work must enter diagnosis or repair before it can be marked complete."

    if target == "delivered" and ro.status != "complete":
        return 409, "Mark work complete before recording vehicle delivery."

    return None


def manual_status_options(ro: RepairOrder, role: str, choices: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the manual status dropdown aligned with the server's guard."""
    return [(key, label) for key, label in choices if status_error(ro, key, role) is None]
