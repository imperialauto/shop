"""Appointment domain rules for Imperial Auto Care.

No database writes or external calendar calls occur in this module.
Times are timezone-aware and normalized to America/Phoenix.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

PHOENIX = ZoneInfo("America/Phoenix")


class AppointmentValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AppointmentWindow:
    starts_at: datetime
    ends_at: datetime
    resource_id: str
    # Time the resource is unavailable around the appointment itself.
    travel_before_minutes: int = 0
    buffer_after_minutes: int = 0

    def __post_init__(self):
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise AppointmentValidationError("Appointment times must include a timezone.")
        if self.starts_at >= self.ends_at:
            raise AppointmentValidationError("Appointment end must follow start.")
        if not self.resource_id.strip():
            raise AppointmentValidationError("A technician or bay is required.")
        if self.travel_before_minutes < 0 or self.buffer_after_minutes < 0:
            raise AppointmentValidationError("Travel and buffer time cannot be negative.")

    @property
    def blocked_from(self) -> datetime:
        return self.starts_at - timedelta(minutes=self.travel_before_minutes)

    @property
    def blocked_until(self) -> datetime:
        return self.ends_at + timedelta(minutes=self.buffer_after_minutes)


def overlaps(left: AppointmentWindow, right: AppointmentWindow) -> bool:
    """Compares blocked time: mobile travel before, plus shop buffer after."""
    return (
        left.resource_id == right.resource_id
        and left.blocked_from < right.blocked_until
        and right.blocked_from < left.blocked_until
    )


def validate_open_hours(window: AppointmentWindow, hours: dict[int, tuple[time, time]]) -> None:
    """Hours are explicit configuration, never assumed to be 9–5."""
    start = window.starts_at.astimezone(PHOENIX)
    end = window.ends_at.astimezone(PHOENIX)
    if start.date() != end.date():
        raise AppointmentValidationError("Appointments must fit within one Phoenix business day.")
    business_hours = hours.get(start.weekday())
    if business_hours is None:
        raise AppointmentValidationError("The shop is closed on the selected day.")
    opens, closes = business_hours
    if not (opens <= start.time().replace(tzinfo=None) and end.time().replace(tzinfo=None) <= closes):
        raise AppointmentValidationError("The appointment is outside configured business hours.")


def validate_slot(
    candidate: AppointmentWindow,
    existing: list[AppointmentWindow],
    hours: dict[int, tuple[time, time]],
) -> None:
    validate_open_hours(candidate, hours)
    if any(overlaps(candidate, item) for item in existing):
        raise AppointmentValidationError("This technician or bay is already booked.")
