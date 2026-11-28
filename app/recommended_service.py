"""Next recommended service: work a customer passed on stays on their file until it's done.

Every line the customer declined, on any job, shows as a next recommended service for that car: on
the customer's page (per vehicle) and on any other job for the same car, where "Add to this job" copies
it onto the estimate as a new line that still needs their approval. A line still waiting on the
customer when the car was picked up counts too, since nobody said yes to it.

A recommendation drops off by itself once the same work shows up on a later line for that car that
wasn't declined (added to a job, quoted again or done), or when the owner removes it. Removing writes
an audit row, so nothing in the jobs or lines changes. Discounts and $0 lines are
left out, and the same work (labor and parts apart) is listed once per car, the newest.

Nothing here texts anyone. Reminders about declined work stay with app.declined_followups (a draft for
the owner 30 to 60 days later).
"""
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.declined_followups import _same_work, _work
from app.dispatch_model import DispatchAudit
from app.models import LineItem, RepairOrder

DISMISSED = "recommendation_dismissed"
ADDED = "recommendation_added"


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _when(line: LineItem) -> datetime:
    return _utc(line.decided_at or line.created_at)


def dismissed_ids(db: Session) -> set[int]:
    ids = set()
    for (detail,) in db.query(DispatchAudit.detail).filter(DispatchAudit.action == DISMISSED).all():
        ids.update(int(n) for n in re.findall(r"\d+", (detail or "").partition("line")[2]))
    return ids


def _counts(line: LineItem) -> bool:
    ro = line.repair_order
    # Close-out marks lines the customer never agreed to as declined by the shop; those still count.
    if ro is None or line.item_type == "discount" or (line.unit_price or 0) <= 0:
        return False
    if line.approval == "declined":
        return True
    # Still waiting on the customer when the car left: nobody said yes to it.
    return line.approval == "pending" and ro.status == "delivered"


def _done_since(line: LineItem, jobs: list[RepairOrder]) -> bool:
    """The same work is on a later line for this car that wasn't declined: added, quoted again or done."""
    since = _when(line)
    vehicle = line.repair_order.vehicle_id
    for job in jobs:
        if vehicle and job.vehicle_id and job.vehicle_id != vehicle:
            continue
        for other in job.line_items:
            if other.id == line.id or other.approval == "declined" or other.item_type != line.item_type:
                continue
            if other.approval == "pending" and job.status == "delivered":
                continue  # left unanswered too; it is a recommendation, not the work done
            if _when(other) > since and _same_work(other.description, line.description):
                return True
    return False


def for_customer(db: Session, customer_id: int) -> list[LineItem]:
    """Open recommendations for one customer, newest first, one per car and kind of work."""
    jobs = db.query(RepairOrder).filter(RepairOrder.customer_id == customer_id).all()
    gone = dismissed_ids(db)
    found: dict[tuple, LineItem] = {}
    for job in jobs:
        for line in job.line_items:
            if line.id in gone or not _counts(line) or _done_since(line, jobs):
                continue
            key = (job.vehicle_id, line.item_type, _work(line.description) or line.description.casefold())
            if key not in found or _when(line) > _when(found[key]):
                found[key] = line
    return sorted(found.values(), key=lambda li: (_when(li), li.id), reverse=True)


def by_vehicle(db: Session, customer_id: int) -> list[tuple]:
    """[(vehicle or None, [lines])] for the customer page."""
    groups: dict = {}
    for line in for_customer(db, customer_id):
        groups.setdefault(line.repair_order.vehicle_id, []).append(line)
    return [(lines[0].repair_order.vehicle, lines) for lines in groups.values()]


def for_job(db: Session, ro: RepairOrder) -> list[LineItem]:
    """Recommendations from this car's other jobs, offered on this job."""
    if not ro.customer_id:
        return []
    return [li for li in for_customer(db, ro.customer_id)
            if li.repair_order_id != ro.id and (not ro.vehicle_id or li.repair_order.vehicle_id in (None, ro.vehicle_id))]


def dismiss(db: Session, line: LineItem, actor: str) -> None:
    db.add(DispatchAudit(item_id=None, actor=actor[:64], action=DISMISSED,
                         detail=f"customer {line.repair_order.customer_id} line {line.id}"))


def add_to_job(db: Session, line: LineItem, ro: RepairOrder, actor: str) -> LineItem:
    """Copies the recommendation onto this job as a new line. It still needs the customer's approval."""
    copy = LineItem(repair_order_id=ro.id, description=line.description, item_type=line.item_type,
                    quantity=line.quantity, unit_price=line.unit_price, taxable=line.taxable,
                    created_at=datetime.utcnow())
    db.add(copy)
    db.flush()
    db.add(DispatchAudit(item_id=None, actor=actor[:64], action=ADDED,
                         detail=f"job {ro.id} from line {line.id} new line {copy.id}"))
    return copy
