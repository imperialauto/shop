"""Read-only view of the owner's Google Calendar through its private iCal address.

CALENDAR_ICS_URL is a secret: anyone holding it can read the calendar, so it is
never logged and errors record only their type. Nothing here writes to Google.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PHOENIX = ZoneInfo("America/Phoenix")
FRESH_SECONDS = 300
STALE_SECONDS = 3600
MAX_OCCURRENCES = 2000
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

log = logging.getLogger("uvicorn.error")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log includes the URL
_cache: dict = {"at": 0.0, "text": None}


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _when(params: dict, value: str) -> tuple[datetime, bool]:
    """Returns a Phoenix datetime and whether the value is an all-day date."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or len(value) == 8:
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=PHOENIX), True
    if value.endswith("Z"):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).astimezone(PHOENIX), False
    try:
        zone = ZoneInfo(params.get("TZID", "America/Phoenix"))
    except (KeyError, ValueError):
        zone = PHOENIX
    return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone).astimezone(PHOENIX), False


def parse_events(text: str) -> list[dict]:
    events, current = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {"exdates": set()}
        elif line == "END:VEVENT":
            if current and current.get("start") and current.get("status") != "CANCELLED":
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *rest = head.split(";")
            params = dict(part.split("=", 1) for part in rest if "=" in part)
            try:
                if name == "DTSTART":
                    current["start"], current["allDay"] = _when(params, value)
                elif name == "DTEND":
                    current["end"] = _when(params, value)[0]
                elif name == "SUMMARY":
                    current["title"] = _unescape(value)
                elif name == "LOCATION":
                    current["location"] = _unescape(value)
                elif name == "STATUS":
                    current["status"] = value.strip().upper()
                elif name == "UID":
                    current["uid"] = value.strip()
                elif name == "RRULE":
                    current["rule"] = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
                elif name == "EXDATE":
                    current["exdates"].update(_when(params, item)[0] for item in value.split(","))
                elif name == "RECURRENCE-ID":
                    current["replaces"] = _when(params, value)[0]
            except ValueError:
                current = None  # one malformed event must not hide the rest
    return events


def _add_months(start: datetime, months: int) -> datetime | None:
    index = start.month - 1 + months
    try:
        return start.replace(year=start.year + index // 12, month=index % 12 + 1)
    except ValueError:
        return None  # e.g. the 31st in a shorter month


def _occurrences(event: dict, until: datetime):
    start, rule = event["start"], event.get("rule")
    if not rule:
        yield start
        return
    frequency = rule.get("FREQ")
    interval = max(int(rule.get("INTERVAL", 1) or 1), 1)
    count = int(rule["COUNT"]) if rule.get("COUNT") else None
    end = until
    if rule.get("UNTIL"):
        end = min(end, _when({}, rule["UNTIL"])[0] + timedelta(days=1 if len(rule["UNTIL"]) == 8 else 0))
    weekdays = {DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAYS}
    if frequency not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        yield start
        return
    if frequency == "MONTHLY" and weekdays:
        yield start  # "second Tuesday" style rules are not expanded
        return
    made = 0
    for step in range(MAX_OCCURRENCES):
        if frequency == "DAILY":
            batch = [start + timedelta(days=step * interval)]
        elif frequency == "WEEKLY":
            base = start + timedelta(weeks=step * interval)
            monday = base - timedelta(days=base.weekday())
            days = sorted(weekdays) if weekdays else [start.weekday()]
            batch = [monday + timedelta(days=day) for day in days]
        else:
            batch = [_add_months(start, step * interval * (1 if frequency == "MONTHLY" else 12))]
        for when in batch:
            if when is None or when < start:
                continue
            if when > end or (count is not None and made >= count):
                return
            made += 1
            yield when


def events_between(text: str, start: datetime, end: datetime) -> list[dict]:
    parsed = parse_events(text)
    moved = {(e.get("uid"), e["replaces"]) for e in parsed if e.get("replaces")}
    found = []
    for event in parsed:
        length = (event.get("end") or event["start"]) - event["start"]
        for when in _occurrences(event, end):
            if when in event["exdates"] or (event.get("rule") and (event.get("uid"), when) in moved):
                continue
            if when < end and (when >= start or when + length > start):
                found.append({"start": when, "allDay": bool(event.get("allDay")),
                              "title": event.get("title") or "Busy", "location": event.get("location") or ""})
    return sorted(found, key=lambda row: row["start"])


def _feed_text() -> str | None:
    url = os.getenv("CALENDAR_ICS_URL", "").strip()
    if not url:
        return None
    age = time.monotonic() - _cache["at"]
    if _cache["text"] is not None and age < FRESH_SECONDS:
        return _cache["text"]
    try:
        import httpx
        response = httpx.get(url, timeout=6, follow_redirects=True)
        response.raise_for_status()
        if "BEGIN:VCALENDAR" not in response.text:
            raise ValueError("not a calendar")
        _cache.update(at=time.monotonic(), text=response.text)
        return response.text
    except Exception as error:  # never str(error): it can contain the secret URL
        log.warning("calendar_feed_failed %s", type(error).__name__)
        return _cache["text"] if _cache["text"] is not None and age < STALE_SECONDS else None


def upcoming(now: datetime, days: int = 7) -> list[dict] | None:
    """Events from the start of today, or None if the calendar cannot be read."""
    text = _feed_text()
    if text is None:
        return None
    start = now.astimezone(PHOENIX).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return events_between(text, start, start + timedelta(days=days))
    except (ValueError, KeyError) as error:
        log.warning("calendar_feed_parse_failed %s", type(error).__name__)
        return None


def describe(event: dict, with_day: bool = True) -> str:
    start = event["start"]
    when = start.strftime("%a %b %-d ") if with_day else ""
    when += "all day" if event["allDay"] else start.strftime("%-I:%M %p")
    return f"{when}: {event['title']}"


def is_on(event: dict, day: date) -> bool:
    return event["start"].date() == day
