"""Owner-only removal of a job that was entered by mistake or for testing.

Only the job and the rows that cannot exist without it are removed. Rows that
merely point at the job (texts, leads, estimate drafts) are kept and unlinked.
A job with an invoice or payment, or one that is finished, is never removed,
and nothing is sent to anyone.
"""
import logging
from datetime import datetime

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

from app import approval_links
from app.dispatch_model import OPEN_STATUSES, DispatchAudit, DispatchItem, DispatchOutbound
from app.models import RepairOrder

log = logging.getLogger(__name__)

# Never delete rows from these tables as a side effect of removing a job.
PROTECTED = {"customers", "vehicles", "users", "invoices", "payments"}
FINISHED = {"complete", "delivered"}
MAX_DEPTH = 4


class DeleteRefused(Exception):
    pass


def blockers(ro: RepairOrder) -> list[str]:
    """Plain reasons this job can't be deleted. Empty means it can."""
    reasons = []
    if ro.invoices:
        reasons.append("It has an invoice. Void or delete the invoice first.")
    if (ro.status or "") in FINISHED:
        reasons.append("The work is marked complete or delivered.")
    return reasons


def _referencing(insp, table):
    """Every (child table, column, nullable) whose foreign key points at `table`."""
    for child in insp.get_table_names():
        for fk in insp.get_foreign_keys(child):
            if fk.get("referred_table") != table:
                continue
            cols = fk.get("constrained_columns") or []
            if len(cols) != 1:
                raise DeleteRefused(f"{child} links to {table} in a way this tool doesn't handle.")
            nullable = next((c["nullable"] for c in insp.get_columns(child) if c["name"] == cols[0]), False)
            yield child, cols[0], nullable


def _remove(db: Session, insp, table: str, ids: list, depth: int, counts: dict):
    if not ids:
        return
    if depth > MAX_DEPTH:
        raise DeleteRefused("This job is linked to too many other records to remove safely.")
    q = db.bind.dialect.identifier_preparer.quote
    for child, col, nullable in list(_referencing(insp, table)):
        where = text(f"SELECT COUNT(*) FROM {q(child)} WHERE {q(col)} IN :ids").bindparams(
            bindparam("ids", expanding=True))
        found = db.execute(where, {"ids": ids}).scalar() or 0
        if not found:
            continue
        if child in PROTECTED:
            raise DeleteRefused(f"It is linked to {child.replace('_', ' ')}, which are never removed here.")
        if nullable:
            db.execute(text(f"UPDATE {q(child)} SET {q(col)} = NULL WHERE {q(col)} IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": ids})
            counts.setdefault("unlinked", {})[child] = counts.get("unlinked", {}).get(child, 0) + found
            continue
        pk = (insp.get_pk_constraint(child) or {}).get("constrained_columns") or []
        if len(pk) == 1:
            child_ids = [r[0] for r in db.execute(text(
                f"SELECT {q(pk[0])} FROM {q(child)} WHERE {q(col)} IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": ids})]
            _remove(db, insp, child, child_ids, depth + 1, counts)
        else:
            db.execute(text(f"DELETE FROM {q(child)} WHERE {q(col)} IN :ids").bindparams(
                bindparam("ids", expanding=True)), {"ids": ids})
            counts.setdefault("deleted", {})[child] = counts.get("deleted", {}).get(child, 0) + found
    pk = (insp.get_pk_constraint(table) or {}).get("constrained_columns") or ["id"]
    result = db.execute(text(f"DELETE FROM {q(table)} WHERE {q(pk[0])} IN :ids").bindparams(
        bindparam("ids", expanding=True)), {"ids": ids})
    counts.setdefault("deleted", {})[table] = counts.get("deleted", {}).get(table, 0) + (result.rowcount or 0)


def delete_repair_order(db: Session, ro: RepairOrder, actor: str) -> dict:
    """Removes one job in a single transaction. Raises DeleteRefused and changes nothing on refusal."""
    reasons = blockers(ro)
    if reasons:
        raise DeleteRefused(" ".join(reasons))
    ro_id = ro.id
    label = f"Job #{ro_id}"
    counts: dict = {}
    try:
        # A queued approval-link text for this job must never go out once the job is gone.
        now = datetime.now().astimezone()
        for item in db.query(DispatchItem).filter(
                DispatchItem.quo_conversation_id == approval_links.conversation_key(ro_id),
                DispatchItem.status.in_(OPEN_STATUSES + ("approved",))).all():
            # Includes a link the owner just tapped to send that hasn't gone out yet.
            for row in db.query(DispatchOutbound).filter(DispatchOutbound.item_id == item.id,
                                                         DispatchOutbound.status == "queued").all():
                row.status = "skipped"
            item.status = "closed"
            item.updated_at = now
            db.add(DispatchAudit(item_id=item.id, actor=actor, action="job_deleted",
                                 detail=f"{label} deleted; approval text cancelled"))
            counts["closed_queue_items"] = counts.get("closed_queue_items", 0) + 1
        db.flush()
        db.expunge(ro)
        insp = inspect(db.connection())
        _remove(db, insp, "repair_orders", [ro_id], 0, counts)
        if counts.get("deleted", {}).get("repair_orders") != 1:
            raise DeleteRefused("The job could not be found to delete.")
        db.add(DispatchAudit(item_id=None, actor=actor, action="job_deleted", detail=label[:255]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    log.info("job_deleted ro_id=%s actor=%s counts=%s", ro_id, actor, counts)
    return counts
