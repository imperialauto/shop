"""Owner-editable business settings. A saved value wins over the Railway variable of the same setting.

Every change keeps the old and new value with who made it. If the table is missing
or unreadable, the Railway variables are used, so the app never fails on settings.
"""
import json
import os
import time
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal

# key -> the Railway variable it replaces
KEYS = {
    "shop_hours": "SHOP_HOURS_JSON",
    "shop_buffer_minutes": "SHOP_BUFFER_MINUTES",
    "booking_services": "PUBLIC_SERVICE_CATALOG",
    "mobile_zones": "PUBLIC_MOBILE_ZONES",
    "booking_lead_minutes": "PUBLIC_BOOKING_LEAD_MINUTES",
    "booking_closed_days": "PUBLIC_BOOKING_CLOSED_DAYS",
    "owner_alert_batch_limit": "OWNER_ALERT_BATCH_LIMIT",
    "alert_rules": "ALERT_RULES_JSON",
    "labor_rates": "LABOR_RATES_JSON",
    "booking_auto_times": "PUBLIC_AUTO_TIMES",
}
CACHE_SECONDS = 15
_cache: dict = {"at": 0.0, "values": None}


class BusinessSetting(Base):
    __tablename__ = "business_settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)
    updated_by = Column(String(64), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class BusinessSettingChange(Base):
    __tablename__ = "business_setting_changes"
    id = Column(Integer, primary_key=True)
    key = Column(String(64), nullable=False, index=True)
    old_value = Column(Text)
    new_value = Column(Text, nullable=False)
    changed_by = Column(String(64), nullable=False)
    changed_at = Column(DateTime(timezone=True), nullable=False)


def _load() -> dict:
    now = time.monotonic()
    if _cache["values"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["values"]
    values = {}
    try:
        with SessionLocal() as db:
            values = {row.key: row.value for row in db.query(BusinessSetting).all()}
    except Exception:  # missing table or no database: the variables still work
        values = {}
    _cache.update(at=now, values=values)
    return values


def clear_cache():
    _cache.update(at=0.0, values=None)


def raw(key: str) -> str | None:
    """The saved text for a setting, else its Railway variable, else None."""
    saved = _load().get(key)
    return saved if saved is not None else os.environ.get(KEYS[key])


OWNER_ALERT_DEFAULT = 10
OWNER_ALERT_MAX = 10


def owner_alert_batch_limit() -> int:
    try:
        value = int(raw("owner_alert_batch_limit") or "")
    except ValueError:
        return OWNER_ALERT_DEFAULT
    return value if 0 <= value <= OWNER_ALERT_MAX else OWNER_ALERT_DEFAULT


def save(db: Session, key: str, value, actor: str) -> bool:
    """Stores the value as text. Returns False when nothing changed. Caller commits."""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    row = db.get(BusinessSetting, key)
    old = row.value if row else os.environ.get(KEYS[key])
    if old == text:
        return False
    now = datetime.now(timezone.utc)
    if row is None:
        db.add(BusinessSetting(key=key, value=text, updated_by=actor, updated_at=now))
    else:
        row.value, row.updated_by, row.updated_at = text, actor, now
    db.add(BusinessSettingChange(key=key, old_value=old, new_value=text, changed_by=actor, changed_at=now))
    return True
