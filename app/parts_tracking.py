"""Parts on a job: ordered, arrived, supplier and ETA, so the job moves on by itself when parts come in.

Rows live in parts_orders. Statuses this card works with:
- sourcing: priced only (from the owner sheet or the assistant); not ordered yet, never holds a job.
- ordered: on its way. Holds a "waiting_parts" job.
- received: arrived (shown as "Arrived").
- returned: sent back; ignored when deciding whether everything is here.

When the last outstanding part arrives on a job that is "waiting_parts", the job moves to
"in_progress" and a DispatchAudit row records it. Nothing here texts anyone.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app import ro_status_policy
from app.dispatch_model import DispatchAudit
from app.models import PartsOrder, RepairOrder

PHOENIX = ZoneInfo("America/Phoenix")
OFFICE = ("owner", "admin", "office")
STAFF = OFFICE + ("tech",)
LABELS = {"sourcing": "Not ordered", "ordered": "Ordered", "received": "Arrived", "returned": "Returned"}
HOLD_FROM = ("approval_received", "in_progress")


def can_edit(role: str | None) -> bool:
    return role in OFFICE


def can_mark_arrived(role: str | None) -> bool:
    return role in STAFF


def today() -> date:
    return datetime.now(PHOENIX).date()


def parts_for(db: Session, ro_id: int) -> list[PartsOrder]:
    return (db.query(PartsOrder).filter(PartsOrder.repair_order_id == ro_id)
            .order_by(PartsOrder.ordered_at.is_(None), PartsOrder.ordered_at, PartsOrder.id).all())


def outstanding(parts: list[PartsOrder]) -> list[PartsOrder]:
    """Ordered and not here yet."""
    return [p for p in parts if p.status == "ordered"]


def all_arrived(parts: list[PartsOrder]) -> bool:
    """True when at least one part was ordered for the job and every one not returned has arrived."""
    tracked = [p for p in parts if p.status in ("ordered", "received")]
    return bool(tracked) and all(p.status == "received" for p in tracked)


def _clean(text: str | None, limit: int) -> str | None:
    text = " ".join((text or "").split())
    return text[:limit] or None


def parse_eta(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("ETA must be a date.")


def parse_cost(value: str | None) -> float:
    value = (value or "").strip().replace("$", "").replace(",", "")
    if not value:
        return 0.0
    try:
        cost = round(float(value), 2)
    except ValueError:
        raise ValueError("Cost must be a number.")
    if cost < 0 or cost > 100000:
        raise ValueError("Cost looks wrong.")
    return cost


def parse_qty(value: str | int | None) -> int:
    try:
        qty = int(value or 1)
    except (TypeError, ValueError):
        raise ValueError("Qty must be a whole number.")
    if qty < 1 or qty > 999:
        raise ValueError("Qty must be between 1 and 999.")
    return qty


def add_part(db: Session, ro: RepairOrder, *, description: str, part_number: str = "", supplier: str = "",
             quantity: str | int | None = 1, cost: str | None = "", eta: str | None = "") -> PartsOrder:
    desc = _clean(description, 500)
    if not desc:
        raise ValueError("Say what the part is.")
    part = PartsOrder(repair_order_id=ro.id, description=desc, part_number=_clean(part_number, 64),
                      vendor=_clean(supplier, 128), quantity=parse_qty(quantity), cost=parse_cost(cost),
                      eta=parse_eta(eta), status="ordered", ordered_at=datetime.utcnow())
    db.add(part)
    db.flush()
    return part


def edit_part(db: Session, part: PartsOrder, *, description: str, part_number: str = "", supplier: str = "",
              quantity: str | int | None = 1, cost: str | None = None, eta: str | None = "") -> PartsOrder:
    desc = _clean(description, 500)
    if not desc:
        raise ValueError("Say what the part is.")
    part.description = desc
    part.part_number = _clean(part_number, 64)
    part.vendor = _clean(supplier, 128)
    part.quantity = parse_qty(quantity)
    if cost is not None:
        part.cost = parse_cost(cost)
    part.eta = parse_eta(eta)
    db.flush()
    return part


def mark_ordered(db: Session, part: PartsOrder) -> PartsOrder:
    """Order a priced part, or undo an Arrived/Returned press made by mistake."""
    if part.status == "sourcing" or part.ordered_at is None:
        part.ordered_at = datetime.utcnow()
    part.status = "ordered"
    part.received_at = None
    db.flush()
    return part


def mark_returned(db: Session, part: PartsOrder) -> PartsOrder:
    part.status = "returned"
    db.flush()
    return part


def mark_arrived(db: Session, ro: RepairOrder, part: PartsOrder, actor: str) -> bool:
    """Mark one part arrived. Returns True when that moved the job on to in_progress."""
    if part.status != "received":
        part.status = "received"
        part.received_at = datetime.utcnow()
    db.flush()
    return advance_if_ready(db, ro, actor)


def advance_if_ready(db: Session, ro: RepairOrder, actor: str) -> bool:
    """Move a waiting_parts job to in_progress once every part not returned has arrived."""
    if ro.status != "waiting_parts" or not all_arrived(parts_for(db, ro.id)):
        return False
    if ro_status_policy.status_error(ro, "in_progress", "owner") is not None:
        return False
    ro.status = "in_progress"
    db.add(DispatchAudit(item_id=None, actor=(actor or "staff")[:64], action="parts_in_job_moved",
                         detail=f"job {ro.id} waiting_parts -> in_progress: all parts arrived"[:255]))
    db.flush()
    return True


def can_offer_hold(ro: RepairOrder, parts: list[PartsOrder]) -> bool:
    """Offer (never force) "waiting_parts" when parts are on order for a job that is ready to work."""
    return (ro.status in HOLD_FROM and bool(outstanding(parts))
            and ro_status_policy.status_error(ro, "waiting_parts", "owner") is None)


def hold_for_parts(db: Session, ro: RepairOrder, actor: str) -> bool:
    parts = parts_for(db, ro.id)
    if not can_offer_hold(ro, parts):
        return False
    previous = ro.status
    ro.status = "waiting_parts"
    db.add(DispatchAudit(item_id=None, actor=(actor or "staff")[:64], action="job_held_for_parts",
                         detail=f"job {ro.id} {previous} -> waiting_parts"[:255]))
    db.flush()
    return True


def _day(value: datetime | None) -> str:
    if value is None:
        return ""
    aware = value.replace(tzinfo=ZoneInfo("UTC")) if value.tzinfo is None else value
    return aware.astimezone(PHOENIX).strftime("%-m/%-d")


def card(db: Session, ro: RepairOrder, role: str | None) -> dict:
    """Everything the job page's Parts card needs."""
    parts = parts_for(db, ro.id)
    now = today()
    rows = []
    for p in parts:
        late = p.status == "ordered" and p.eta is not None and p.eta < now
        rows.append({
            "id": p.id, "status": p.status, "label": LABELS.get(p.status, (p.status or "").replace("_", " ").title()),
            "description": p.description or "Part", "part_number": p.part_number or "", "supplier": p.vendor or "",
            "quantity": p.quantity or 1, "cost": p.cost or 0.0,
            "ordered": _day(p.ordered_at) if p.status != "sourcing" else "",
            "arrived": _day(p.received_at) if p.status == "received" else "",
            "eta": p.eta.strftime("%-m/%-d") if p.eta else "", "eta_iso": p.eta.isoformat() if p.eta else "",
            "late": late,
        })
    waiting = outstanding(parts)
    return {
        "rows": rows,
        "waiting": len(waiting),
        "late": sum(1 for r in rows if r["late"]),
        "can_edit": can_edit(role),
        "can_arrive": can_mark_arrived(role),
        "show_cost": role in OFFICE,
        "offer_hold": can_edit(role) and can_offer_hold(ro, parts),
        "stuck": ro.status == "waiting_parts" and not waiting,
    }
