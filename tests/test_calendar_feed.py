"""Calendar parsing, on synthetic events only."""
import unittest
import os
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.calendar_feed import describe, events_between, feed_urls, parse_events

PHX = ZoneInfo("America/Phoenix")
FEED = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:one
DTSTART;TZID=America/Phoenix:20260105T110000
DTEND;TZID=America/Phoenix:20260105T120000
SUMMARY:Sample inspection\\, shop
LOCATION:Suite 1
END:VEVENT
BEGIN:VEVENT
UID:utc
DTSTART:20260105T210000Z
DTEND:20260105T220000Z
SUMMARY:Long title that keeps
  going
END:VEVENT
BEGIN:VEVENT
UID:day
DTSTART;VALUE=DATE:20260106
DTEND;VALUE=DATE:20260107
SUMMARY:All day thing
END:VEVENT
BEGIN:VEVENT
UID:gone
STATUS:CANCELLED
DTSTART:20260105T210000Z
SUMMARY:Cancelled
END:VEVENT
BEGIN:VEVENT
UID:weekly
DTSTART;TZID=America/Phoenix:20251201T090000
DTEND;TZID=America/Phoenix:20251201T100000
RRULE:FREQ=WEEKLY;BYDAY=MO,WE
EXDATE;TZID=America/Phoenix:20260107T090000
SUMMARY:Standing
END:VEVENT
BEGIN:VEVENT
UID:weekly
RECURRENCE-ID;TZID=America/Phoenix:20260105T090000
DTSTART;TZID=America/Phoenix:20260105T150000
DTEND;TZID=America/Phoenix:20260105T160000
SUMMARY:Standing moved
END:VEVENT
BEGIN:VEVENT
UID:ended
DTSTART:20250101T170000Z
RRULE:FREQ=DAILY;COUNT=3
SUMMARY:Ended series
END:VEVENT
END:VCALENDAR
"""


class CalendarFeedTests(unittest.TestCase):
    def window(self, day, days=1):
        start = datetime(2026, 1, day, tzinfo=PHX)
        return events_between(FEED, start, start.replace(day=day + days))

    def test_times_convert_to_phoenix_and_text_is_cleaned(self):
        rows = self.window(5)
        self.assertEqual([describe(r, False) for r in rows], [
            "11:00 AM: Sample inspection, shop", "2:00 PM: Long title that keeps going", "3:00 PM: Standing moved"])

    def test_cancelled_events_are_left_out(self):
        self.assertNotIn("Cancelled", [e.get("title") for e in parse_events(FEED)])

    def test_all_day_events_and_excluded_repeats(self):
        self.assertEqual([describe(r, False) for r in self.window(6)], ["all day: All day thing"])
        self.assertEqual(self.window(7), [])

    def test_weekly_repeat_lands_on_each_listed_day(self):
        self.assertEqual([describe(r, False) for r in self.window(12)], ["9:00 AM: Standing"])
        self.assertEqual([describe(r, False) for r in self.window(14)], ["9:00 AM: Standing"])

    def test_finished_series_does_not_appear(self):
        self.assertNotIn("Ended series", [r["title"] for r in self.window(5, 20)])

    def test_bad_event_does_not_hide_others(self):
        bad = FEED.replace("BEGIN:VCALENDAR\n", "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:garbage\nEND:VEVENT\n")
        self.assertEqual(len(parse_events(bad)), len(parse_events(FEED)))

    def test_apple_and_google_addresses_are_accepted_together(self):
        with patch.dict(os.environ, {"CALENDAR_ICS_URL": "webcal://apple.example/a , https://google.example/b.ics"}):
            self.assertEqual(feed_urls(), ["https://apple.example/a", "https://google.example/b.ics"])

    def test_two_feeds_end_to_end_both_count(self):
        self.assertEqual(len(parse_events(FEED + FEED)), 2 * len(parse_events(FEED)))


if __name__ == "__main__":
    unittest.main()
