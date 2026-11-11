"""Server-side guardrails for staff repair-order status changes.

Customer approvals are data, not a dropdown state: staff cannot substitute a
status change for a signed/recorded line decision. The customer-signature and
estimate-approval routes remain independent and keep their existing semantics.

This module has no side effects, database writes, or outbound messaging.
"""
from app.models import RepairOrder


WORK_TARGETS = frozenset(("in_progress", "waiting_parts", "complete"))
FINISH_FROM = frozenset(("diagnosing", "in_progress", "waiting_parts", "complete"))


def status_error(ro: RepairOrder, target: str, role: str) -> tuple[int, str] | None:
    """Return (HTTP code, user-facing explanation) or None if permitted."""
    if target == ro.status:
        return None

    if role == "tech" and target in ("waiting_approval", "approval_received", "delivered"):
        return 403, "Customer approvals and vehicle release are handled by the office."

    lines = ro.line_items
    approved = any(line.approval == "approved" for line in lines)
    unresolved = any(line.approval in (None, "pending") for line in lines)

    if target == "waiting_approval" and not any(line.approval == "pending" for line in lines):
        return 409, "Use Ask for approval to queue the estimate and record pending lines first."

    if target == "approval_received" and (
        not lines or unresolved or not any(line.approval in ("approved", "declined") for line in lines)
    ):
        return 409, "Record the customer's decision on each quoted line before marking approval received."

    if target in WORK_TARGETS and unresolved and not approved:
        return 409, "No quoted work has been approved. Review the estimate before starting or completing repairs."

    if target == "complete" and ro.status not in FINISH_FROM:
        return 409, "Work must enter diagnosis or repair before it can be marked complete."

    if target == "delivered" and ro.status != "complete":
        return 409, "Mark work complete before recording vehicle delivery."

    return None


def manual_status_options(ro: RepairOrder, role: str, choices: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the manual status dropdown aligned with the server's guard."""
    return [(key, label) for key, label in choices if status_error(ro, key, role) is None]
