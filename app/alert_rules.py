"""Owner alert rules. New customer messages can be delayed by quiet hours but never turned off."""
import json
from datetime import datetime

from app import business_settings

OPTIONAL = {
    "owner_daily": "Morning summary (10 AM)",
    "owner_wrap": "End-of-day wrap-up (6:30 PM)",
    "owner_nudge": "Follow-up reminders on unanswered items",
    "owner_calendar": "Heads-up before calendar appointments",
    "owner_stuck": "Daily note on jobs stuck for 2+ days",
    "owner_leads": "Open-lead reminders (at most every 30 minutes in shop hours)",
    "website_booking": "Note when a customer books online",
}
LABELS = {
    "owner_alert": "Customer message", "owner_nudge": "Follow-up reminder", "owner_wrap": "End-of-day wrap-up",
    "owner_daily": "Morning summary", "owner_calendar": "Appointment heads-up", "owner_backup": "Backup problem",
    "owner_stuck": "Stuck jobs", "owner_leads": "Open leads",
    "dashboard_reply": "Reply sent from dashboard",
}
DEFAULTS = {"quiet_start": "", "quiet_end": "", "off": []}


def rules() -> dict:
    try:
        saved = json.loads(business_settings.raw("alert_rules") or "{}")
    except ValueError:
        saved = {}
    merged = {**DEFAULTS, **(saved if isinstance(saved, dict) else {})}
    merged["off"] = [kind for kind in merged.get("off") or [] if kind in OPTIONAL]
    return merged


def in_quiet_hours(now: datetime, current: dict | None = None) -> bool:
    current = current or rules()
    start, end = current.get("quiet_start"), current.get("quiet_end")
    if not start or not end or start == end:
        return False
    clock = now.strftime("%H:%M")
    return start <= clock < end if start < end else clock >= start or clock < end


def allowed(kind: str, urgent: bool, website_booking: bool, now: datetime, current: dict | None = None) -> bool:
    """Urgent items and backup problems always go out. Everything else follows the owner's rules."""
    current = current or rules()
    if urgent or kind == "owner_backup":
        return True
    if kind in current["off"] or (website_booking and "website_booking" in current["off"]):
        return False
    return not in_quiet_hours(now, current)
