"""What runs when the owner moves a job to a new status.

One place for status-driven automations so they don't overlap. Each handler gets (db, ro) and runs
inside its own savepoint, so one failing never blocks the status change or the other handlers.

- complete: the ready-for-pickup text (app.pickup_texts).
- delivered: the Google review request for the day after pickup (app.review_requests). Payments
  recorded after pickup schedule it from app.routes.invoices. The pickup-ready text goes out on
  complete and never asks for a review, so a customer gets one text at each step.
"""
from sqlalchemy.orm import Session

from app import pickup_texts, review_requests
from app.models import RepairOrder

HANDLERS = {
    "complete": [pickup_texts.queue],
    "delivered": [review_requests.schedule],
}


def fire(db: Session, ro: RepairOrder, status: str) -> None:
    for handler in HANDLERS.get(status, []):
        try:
            with db.begin_nested():
                handler(db, ro)
        except Exception as exc:
            print(f"job_event_failed status={status} job={ro.id} {getattr(handler, '__module__', '')} {type(exc).__name__}")
