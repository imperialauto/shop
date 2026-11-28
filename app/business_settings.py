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
    "task_followup_days": "TASK_FOLLOWUP_DAYS",
    "automatic_customer_followups": "AUTOMATIC_CUSTOMER_FOLLOWUPS",
    "next_visit_offer": "NEXT_VISIT_OFFER",
    "automatic_review_text": "AUTOMATIC_REVIEW_TEXT",
    "google_review_link": "GOOGLE_REVIEW_LINK",
    "automatic_deposit_text": "AUTOMATIC_DEPOSIT_TEXT",
    "automatic_pickup_text": "AUTOMATIC_PICKUP_TEXT",
    "automatic_oil_reminders": "AUTOMATIC_OIL_REMINDERS",
    "oil_reminder_months": "OIL_REMINDER_MONTHS",
    "automatic_declined_followup": "AUTOMATIC_DECLINED_FOLLOWUP",
    "automatic_declined_reminder": "AUTOMATIC_DECLINED_REMINDER",
    "canned_jobs": "CANNED_JOBS_JSON",
    "automatic_diag_credit_reminder": "AUTOMATIC_DIAG_CREDIT_REMINDER",
    "automatic_snap_link": "AUTOMATIC_SNAP_LINK",
    "snap_apply_link": "SNAP_APPLY_LINK",
    # Snap Finance on the public website (app/site_financing.py): "false" hides it everywhere.
    "website_financing": "WEBSITE_FINANCING",
    "alert_rules": "ALERT_RULES_JSON",
    "labor_rates": "LABOR_RATES_JSON",
    "booking_auto_times": "PUBLIC_AUTO_TIMES",
    "security_controls": "SECURITY_CONTROLS_JSON",
    "payment_methods": "PAYMENT_METHODS",
    "legal_review": "LEGAL_REVIEW_JSON",
    # Hash of the owner's private calendar-feed address (app/appointment_feed.py). Set in Settings only.
    "appointment_feed": "APPOINTMENT_FEED_SETTING",
    # Money to bring in each day: shared by the Shop TV board (/board/manage) and the Money page.
    "daily_sales_goal": "DAILY_SALES_GOAL",
    # Which daily goal Home, the scoreboard and the TV use: "fixed" (daily_sales_goal, the default) or "bills"
    # (what the bills need a day when that is more than zero). Set on /board/manage.
    "daily_goal_mode": "DAILY_GOAL_MODE",
    "finance_pay_order": "FINANCE_PAY_ORDER_JSON",
    "monthly_profit_goal": "MONTHLY_PROFIT_GOAL",
    # Profit goal for May through September; 0 means use the monthly goal all year.
    "summer_profit_goal": "SUMMER_PROFIT_GOAL",
    # Shop TV board: which open jobs' cars are at the shop. Set on /board/manage.
    "board_on_lot": "BOARD_ON_LOT_JSON",
    "board_timers": "BOARD_TIMERS_JSON",
    "board_job_clock": "BOARD_JOB_CLOCK_JSON",
    # Website launch offer (app/launch_offer.py): {"on": false} turns it off.
    "launch_offer": "LAUNCH_OFFER_JSON",
    # "true" texts a booking confirmation to website customers who chose text (app/booking_confirmations.py).
    "booking_confirmation_texts": "BOOKING_CONFIRMATION_TEXTS",
    # Owner's yes / not now on each code change, set on the Changes page (app/change_review.py).
    "change_approvals": "CHANGE_APPROVALS_JSON",
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
TASK_FOLLOWUP_DEFAULT = 1
TASK_FOLLOWUP_MAX = 30


def owner_alert_batch_limit() -> int:
    try:
        value = int(raw("owner_alert_batch_limit") or "")
    except ValueError:
        return OWNER_ALERT_DEFAULT
    return value if 0 <= value <= OWNER_ALERT_MAX else OWNER_ALERT_DEFAULT


def task_followup_days() -> int:
    try:
        value = int(raw("task_followup_days") or "")
    except ValueError:
        return TASK_FOLLOWUP_DEFAULT
    return value if 1 <= value <= TASK_FOLLOWUP_MAX else TASK_FOLLOWUP_DEFAULT


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
