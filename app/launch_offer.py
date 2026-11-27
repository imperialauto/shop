"""Website launch offer: a percent off labor for the first few customers who book or request online.

On by default: 20% off labor for the first 3. The setting launch_offer (JSON) changes it:
{"on": false} stops it, {"on": true, "percent": 20, "limit": 3} sets it. Roadside requests don't count.

Nothing is discounted automatically. The job's concern and the owner's queue item say to take it off
labor on the final bill, so the owner still sets every price. One per customer, matched by phone.
"""
import json
import re
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import business_settings
from app.models import RepairOrder

SETTING = "launch_offer"
MARK = "Launch offer:"
DEFAULTS = {"on": True, "percent": 20, "limit": 3}
LOCK_KEY = 2026112501  # one claim at a time, so the last spot can't go to two people
BANNER_SECONDS = 30
_banner: dict = {"at": -1e9, "value": None}


def config() -> dict:
    try:
        saved = json.loads(business_settings.raw(SETTING) or "{}")
    except ValueError:
        saved = {}
    merged = {**DEFAULTS, **(saved if isinstance(saved, dict) else {})}
    try:
        percent, limit = int(merged["percent"]), int(merged["limit"])
    except (TypeError, ValueError):
        return {**DEFAULTS, "on": False}
    on = merged.get("on") is True and 1 <= percent <= 50 and 1 <= limit <= 50
    return {"on": on, "percent": percent, "limit": limit}


def _claimed(db: Session) -> list[RepairOrder]:
    return db.query(RepairOrder).filter(RepairOrder.concern.like(f"%{MARK}%")).all()


def _digits(phone) -> str:
    return re.sub(r"\D", "", str(phone or ""))[-10:]


def spots_left(db: Session, current: dict | None = None) -> int:
    current = current or config()
    if not current["on"]:
        return 0
    return max(current["limit"] - len(_claimed(db)), 0)


def claim(db: Session, phone: str) -> str:
    """Call inside the booking's transaction. Returns the line for the job's concern, or "" when the
    offer is off, every spot is taken, or this phone number already has one."""
    current = config()
    if not current["on"]:
        return ""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
    claimed = _claimed(db)
    if len(claimed) >= current["limit"]:
        return ""
    mine = _digits(phone)
    if mine and any(_digits(order.customer.phone if order.customer else "") == mine for order in claimed):
        return ""
    return (f"{MARK} {current['percent']}% off labor (online customer {len(claimed) + 1} of {current['limit']}). "
            "Take it off labor on the final bill. Parts and tax stay full price.")


def note(line: str) -> str:
    """The short version for the owner's queue item."""
    return f" Launch offer: {config()['percent']}% off labor." if line else ""


def banner() -> dict | None:
    """What the site banner shows, or None. Read at most every BANNER_SECONDS; never breaks a page."""
    now = time.monotonic()
    if now - _banner["at"] < BANNER_SECONDS:
        return _banner["value"]
    value = None
    try:
        from app.database import SessionLocal
        current = config()
        if current["on"]:
            with SessionLocal() as db:
                left = spots_left(db, current)
            if left:
                value = {"percent": current["percent"], "limit": current["limit"], "left": left}
    except Exception:  # a settings or database problem hides the banner, never the page
        value = None
    _banner.update(at=now, value=value)
    return value


def forget() -> None:
    """After a claim, so the banner's count is fresh on the next page."""
    _banner["at"] = -1e9
