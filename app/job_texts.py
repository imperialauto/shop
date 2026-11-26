"""The customer's text conversation, shown read-only on the job page.

Built from the texts already saved on the dashboard (Communication rows), so opening a job never calls
Quo. Newest at the bottom; the last SHOWN are open and the rest sit behind "Show all".
"""
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Communication, RepairOrder

SHOWN = 30


def for_job(db: Session, ro: RepairOrder) -> dict:
    from app.approval_links import greeting_for
    filters = [Communication.repair_order_id == ro.id]
    if ro.customer_id:
        filters.append(Communication.customer_id == ro.customer_id)
    rows = (db.query(Communication).filter(or_(*filters))
            .order_by(Communication.created_at.asc(), Communication.id.asc()).all())
    first = greeting_for(ro) if ro.customer else "there"
    return {
        "name": first if first != "there" else "the customer",
        "older": rows[:-SHOWN] if len(rows) > SHOWN else [],
        "recent": rows[-SHOWN:],
        "total": len(rows),
        "this_job": sum(1 for r in rows if r.repair_order_id == ro.id),
    }
