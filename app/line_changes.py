"""Changing estimate lines after the customer saw or approved them, and asking again for just the changes.

The owner (or office) can edit or take off a line that was already sent for approval, approved or
declined, on an open job with no invoice. Nothing is texted from here:

- an edited line goes back to "pending" (waiting on the customer) and keeps the version they last saw
  or answered in `changed_from`, so their approval page can show "Changed, was $X";
- a line added after the customer last answered shows to them as "New";
- a line taken off that they had approved or been asked about is listed on their page as "Removed";
- lines nobody touched keep their approval, so the customer only answers what changed.

Texting them the link again stays a button the owner presses (approval_links.send_now picks up the
changes and uses the "we updated your estimate" wording). Undoing an edit back to exactly what the
customer had approved puts the approval back.
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.dispatch_model import DispatchAudit
from app.models import LineItem, RepairOrder

FIELDS = ("description", "item_type", "quantity", "unit_price", "taxable")
EDITABLE_TYPES = ("labor", "part", "sublet", "misc")
SENT = ("pending", "approved", "declined")
# Board columns a job moves back to "Waiting approval" from when changes go out. Work already
# under way on approved lines (in progress, waiting parts) keeps its column.
REASK_FROM = ("intake", "diagnosing", "approval_received", "waiting_approval")
WAS_LABEL = {"approved": "approved", "pending": "waiting on you", "declined": "declined"}


class LineChangeError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _utc(dt):
    """Naive UTC for comparing created_at (naive) with decided_at (aware on Postgres, naive on SQLite)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _open_invoice(ro: RepairOrder):
    return next((inv for inv in ro.invoices if inv.status != "void"), None)


def edit_problem(li: LineItem) -> LineChangeError | None:
    """Why this line can't be edited or taken off right now, or None."""
    ro = li.repair_order
    if li.item_type == "discount":
        return LineChangeError(409, "This is a discount line. Take it off and add the new amount instead.")
    if li.completed_at:
        return LineChangeError(409, "This line is marked done. Reopen it first if it really should change.")
    if ro.status == "delivered":
        return LineChangeError(409, "This job was picked up. Charges can't change now.")
    if _open_invoice(ro) is not None:
        return LineChangeError(409, "This job has an invoice. Void it first if the charges really need to change.")
    return None


def can_edit(li: LineItem) -> bool:
    return edit_problem(li) is None


def snapshot(li: LineItem) -> dict:
    decided = li.decided_at.isoformat() if li.decided_at else None
    return {"description": li.description, "item_type": li.item_type, "quantity": li.quantity,
            "unit_price": li.unit_price, "taxable": bool(li.taxable), "total": round(li.total, 2),
            "approval": li.approval, "decided_at": decided, "approval_source": li.approval_source,
            "approval_recorded_by": li.approval_recorded_by}


def was(li: LineItem) -> dict | None:
    """The version the customer last saw or answered, while the line waits on them again."""
    if li.approval != "pending" or not li.changed_from:
        return None
    try:
        data = json.loads(li.changed_from)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _same(li: LineItem, data: dict) -> bool:
    return all(data.get(f) == (bool(getattr(li, f)) if f == "taxable" else getattr(li, f)) for f in FIELDS)


def _money(value) -> str:
    return f"${float(value or 0):,.2f}"


def revise(db: Session, li: LineItem, actor: str, **values) -> dict:
    """Saves new values on a line. Caller commits.

    Returns {"changed": bool, "back_to_customer": bool, "restored": bool}. A line the customer was
    asked about goes back to waiting on them; nothing is texted. Raises LineChangeError.
    """
    problem = edit_problem(li)
    if problem:
        raise problem
    new = {k: v for k, v in values.items() if k in FIELDS and v is not None}
    if "description" in new:
        new["description"] = str(new["description"]).strip()
        if not new["description"]:
            raise LineChangeError(422, "Give the line a description.")
    if "item_type" in new and new["item_type"] not in EDITABLE_TYPES:
        raise LineChangeError(422, "Pick labor, part, sublet or misc.")
    if "quantity" in new and not (float(new["quantity"]) > 0):
        raise LineChangeError(422, "Quantity has to be more than 0.")
    if "unit_price" in new and float(new["unit_price"]) < 0 and new.get("item_type", li.item_type) != "misc":
        raise LineChangeError(422, "Only a misc line can have a negative price.")
    if not any((bool(getattr(li, k)) != bool(v)) if k == "taxable" else getattr(li, k) != v for k, v in new.items()):
        return {"changed": False, "back_to_customer": False, "restored": False}

    before = f"'{li.description[:60]}' {_money(li.total)}"
    prior = li.approval
    if prior in SENT and not was(li):
        li.changed_from = json.dumps(snapshot(li))
    for k, v in new.items():
        setattr(li, k, v)
    result = {"changed": True, "back_to_customer": False, "restored": False}
    if prior in SENT:
        old = was(li) or json.loads(li.changed_from)
        if _same(li, old):
            # Changed back to exactly what the customer last saw: put their answer back.
            li.approval = old.get("approval")
            li.decided_at = datetime.fromisoformat(old["decided_at"]) if old.get("decided_at") else None
            li.approval_source = old.get("approval_source")
            li.approval_recorded_by = old.get("approval_recorded_by")
            li.changed_from, li.changed_at = None, None
            result["restored"] = True
        else:
            li.approval, li.decided_at = "pending", None
            li.approval_source, li.approval_recorded_by = None, None
            li.changed_at = datetime.now(timezone.utc)
            result["back_to_customer"] = True
    after = f"'{li.description[:60]}' {_money(li.total)}"
    state = WAS_LABEL.get(prior, "not sent yet") if prior else "not sent yet"
    db.add(DispatchAudit(item_id=None, actor=(actor or "staff")[:64], action="line_changed",
                         detail=f"Job #{li.repair_order_id}: {before} -> {after} (was {state})"[:255]))
    return result


def _removed(ro: RepairOrder) -> list[dict]:
    try:
        data = json.loads(ro.removed_since_approval or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def remove_sent(db: Session, li: LineItem, actor: str) -> dict:
    """Takes off a line that was sent for approval, approved or declined. Caller commits.

    Lines the customer approved or was waiting to answer are listed on their approval page as removed
    until they next answer. Nothing is texted. Raises LineChangeError.
    """
    problem = edit_problem(li)
    if problem:
        raise problem
    if li.approval is None:
        raise LineChangeError(409, "This line wasn't sent for approval. Use the regular delete.")
    ro = li.repair_order
    old = was(li) or {}
    shown_as = old.get("approval") or li.approval
    if shown_as in ("approved", "pending"):
        listed = _removed(ro)
        listed.append({"description": (old.get("description") or li.description)[:200],
                       "total": round(old.get("total", li.total), 2), "was": shown_as})
        ro.removed_since_approval = json.dumps(listed[-30:])
    removed = {"id": li.id, "description": li.description, "total": round(li.total, 2), "approval": li.approval}
    detail = f"Job #{ro.id}: removed line '{li.description}' {_money(li.total)} ({WAS_LABEL.get(li.approval, li.approval)})"
    db.delete(li)
    db.flush()
    db.refresh(ro)
    from app import approval_links
    if not any(x.approval == "pending" for x in ro.line_items):
        # Nothing is left for the customer to answer, so a queued approval text must not go out.
        approval_links.close_for(db, ro, actor=(actor or "staff")[:64], action="line_removed")
        if ro.status == "waiting_approval":
            # Nothing left to wait on: go on with approved work, or back to building the estimate.
            approved = any(x.approval == "approved" for x in ro.line_items)
            ro.status = "approval_received" if approved else "diagnosing"
    db.add(DispatchAudit(item_id=None, actor=(actor or "staff")[:64], action="line_removed", detail=detail[:255]))
    return removed


def answered(ro: RepairOrder) -> None:
    """The customer (or staff for them) answered: the removed-lines list has been seen."""
    if not any(li.approval == "pending" for li in ro.line_items):
        ro.removed_since_approval = None


def _last_answer(ro: RepairOrder):
    times = [_utc(li.decided_at) for li in ro.line_items
             if li.approval in ("approved", "declined") and li.approval_source != "shop" and li.decided_at]
    return max(times) if times else None


def summary(ro: RepairOrder) -> dict:
    """What changed since the customer last answered: changed and new lines (still to answer),
    removed lines, and how many approved lines stay approved."""
    last = _last_answer(ro)
    changed, new = [], []
    for li in ro.line_items:
        if li.item_type == "discount" or li.approval not in (None, "pending"):
            continue
        old = was(li)
        if old:
            changed.append({"line": li, "was": old, "was_label": WAS_LABEL.get(old.get("approval"), ""),
                            "description_changed": old.get("description") != li.description,
                            "price_changed": round(old.get("total", 0), 2) != round(li.total, 2)})
        elif last is not None and _utc(li.created_at) is not None and _utc(li.created_at) > last:
            new.append(li)
    kept = sum(1 for li in ro.line_items if li.approval == "approved" and li.item_type != "discount")
    return {"changed": changed, "new": new, "removed": _removed(ro), "kept": kept,
            "changed_ids": {c["line"].id for c in changed}, "new_ids": {li.id for li in new}}


def has_changes(ro: RepairOrder) -> bool:
    """True when there is something for the customer to answer that changed since they last answered."""
    s = summary(ro)
    return bool(s["changed"] or s["new"])


def revision_key(ro: RepairOrder) -> str:
    """Same changes, same key: the same set of changes is never texted twice."""
    lines = sorted((li.id, li.description, li.item_type, li.quantity, li.unit_price)
                   for li in ro.line_items if li.approval in (None, "pending"))
    raw = json.dumps([lines, _removed(ro)], default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def message(ro: RepairOrder, offer_snap: bool = False) -> str:
    from app import approval_links, snap_offer
    s = summary(ro)
    count = len(s["changed"]) + len(s["new"])
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else "vehicle"
    kept = (" Items you already approved stay approved." if s["kept"] else "")
    text = (f"Hi {approval_links.greeting_for(ro)}, we updated the estimate for your {car}. "
            f"{count} item{'s' if count != 1 else ''} need{'s' if count == 1 else ''} your OK.{kept} "
            f"Review and approve here: {approval_links.base_url()}/status/{ro.public_token} "
            "Reply here with any questions. - Imperial Auto Care")
    return snap_offer.add(text, offer_snap)
