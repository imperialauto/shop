"""Names for unnamed customers, found in their own Quo texts and calls (R08, R38).

Only customers still called "Unknown" are touched. Names you saved in Quo, or
typed in Imperial, are never changed. The messaging service returns a name only
with the customer's exact words as proof; that quote is kept in the customer's
notes. Runs a few customers at a time in the background, so history fills in
on its own and new numbers are checked as they text or call.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.models import Customer

BATCH = 4
EVERY_SECONDS = 120
RECHECK_AFTER = timedelta(days=7)
_lock = threading.Lock()
_last_sweep = None  # monotonic time of the last sweep; None until the first one


def unnamed(customer: Customer) -> bool:
    return (customer.first_name or "").strip().lower() in ("", "unknown")


def ask_bridge(phone: str):
    """POST /find-name on the messaging service. Returns (result, problem)."""
    base = os.environ.get("MESSAGING_BRIDGE_URL", "").rstrip("/")
    token = os.environ.get("OPERATIONS_BRIDGE_TOKEN", "")
    if not base or not token:
        return None, "not_connected"
    try:
        r = httpx.post(f"{base}/api/operations/find-name", json={"phone": phone},
                       headers={"Authorization": f"Bearer {token}"}, timeout=60.0)
        if r.status_code != 200:
            return None, "unavailable"
        return r.json(), None
    except (httpx.HTTPError, ValueError):
        return None, "unavailable"


def check(db: Session, customer: Customer, ask=None) -> str:
    """Looks once. Returns found, none, skipped or unavailable. Commits nothing; the caller does."""
    ask = ask or ask_bridge
    if not unnamed(customer) or not customer.phone:
        return "skipped"
    result, problem = ask(customer.phone)
    if problem:
        return "unavailable"  # not marked as checked, so it's tried again on a later sweep
    if result.get("error"):
        return "unavailable"  # the AI was down: not marked, so it's tried again
    customer.name_checked_at = datetime.now(timezone.utc)
    name = result.get("name")
    if not name or not name.get("first"):
        return "none"
    customer.first_name = name["first"][:64]
    customer.last_name = (name.get("last") or "-")[:64]
    customer.name_source = (name.get("source") or "text")[:24]
    proof = f'Name found in a {customer.name_source} on {datetime.now().strftime("%b %-d, %Y")}: "{name.get("quote", "")}"'
    customer.notes = f"{customer.notes}\n{proof}" if customer.notes else proof
    return "found"


def due(db: Session, limit: int) -> list[Customer]:
    cutoff = datetime.now(timezone.utc) - RECHECK_AFTER
    rows = [c for c in db.query(Customer).filter(Customer.first_name.in_(("Unknown", "unknown", ""))).all()
            if c.phone and (c.name_checked_at is None or _aware(c.name_checked_at) < cutoff)]
    rows.sort(key=lambda c: (c.name_checked_at is not None, _aware(c.name_checked_at) if c.name_checked_at else datetime.min.replace(tzinfo=timezone.utc)))
    return rows[:limit]


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def sweep(session_factory, ask=None, limit: int = BATCH) -> dict:
    """Checks a few unnamed customers. Safe to call often: one sweep at a time, at most every two minutes."""
    global _last_sweep
    if not _lock.acquire(blocking=False):
        return {"skipped": "busy"}
    try:
        if _last_sweep is not None and time.monotonic() - _last_sweep < EVERY_SECONDS:
            return {"skipped": "recent"}
        _last_sweep = time.monotonic()
        tally = {"found": 0, "none": 0, "unavailable": 0, "skipped": 0}
        with session_factory() as db:
            for customer in due(db, limit):
                outcome = check(db, customer, ask)
                tally[outcome] += 1
                db.commit()
                if outcome == "unavailable":
                    break  # Quo or the AI is down; stop and try again next sweep
        return tally
    finally:
        _lock.release()


def progress(db: Session) -> dict:
    rows = db.query(Customer).filter(Customer.first_name.in_(("Unknown", "unknown", ""))).all()
    return {"unnamed": len(rows), "waiting": sum(1 for c in rows if c.phone and c.name_checked_at is None)}
