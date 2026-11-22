"""What runs when the owner moves a job to a new status.

One place for status-driven automations so they don't overlap. Each handler gets (db, ro) and runs
inside its own savepoint, so one failing never blocks the status change or the other handlers.

- complete: the ready-for-pickup text (app.pickup_texts).
- delivered: free for the review request and repeat-customer offers. The Google review request
  still runs from repair_orders.update_status behind REVIEW_REQUESTS_ENABLED; move it here when
  that work is built, so a customer never gets two texts at pickup.
"""
from sqlalchemy.orm import Session

from app import pickup_texts
from app.models import RepairOrder

HANDLERS = {
    "complete": [pickup_texts.queue],
    "delivered": [],
}


def fire(db: Session, ro: RepairOrder, status: str) -> None:
    for handler in HANDLERS.get(status, []):
        try:
            with db.begin_nested():
                handler(db, ro)
        except Exception as exc:
            print(f"job_event_failed status={status} job={ro.id} {getattr(handler, '__module__', '')} {type(exc).__name__}")
