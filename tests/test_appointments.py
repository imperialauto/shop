import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.appointments import AppointmentWindow, AppointmentValidationError, overlaps, validate_slot

AZ = ZoneInfo("America/Phoenix")
HOURS = {0: (time(8), time(18)), 1: (time(8), time(18))}

def slot(day, start, end, resource="bay-1"):
    return AppointmentWindow(datetime(2026, 11, day, start, tzinfo=AZ), datetime(2026, 11, day, end, tzinfo=AZ), resource)

class AppointmentRulesTest(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(overlaps(slot(2, 10, 12), slot(2, 11, 13)))
    def test_back_to_back(self):
        self.assertFalse(overlaps(slot(2, 10, 12), slot(2, 12, 14)))
    def test_independent_bays(self):
        self.assertFalse(overlaps(slot(2, 10, 12), slot(2, 10, 12, "bay-2")))
    def test_conflict_rejected(self):
        with self.assertRaises(AppointmentValidationError):
            validate_slot(slot(2, 11, 13), [slot(2, 10, 12)], HOURS)
    def test_closed_day_rejected(self):
        with self.assertRaises(AppointmentValidationError):
            validate_slot(slot(4, 10, 11), [], HOURS)
    def test_early_rejected(self):
        with self.assertRaises(AppointmentValidationError):
            validate_slot(slot(2, 7, 9), [], HOURS)
    def test_no_default_hours(self):
        with self.assertRaises(AppointmentValidationError):
            validate_slot(slot(2, 10, 11), [], {})
    def test_invalid_order(self):
        with self.assertRaises(AppointmentValidationError):
            slot(2, 12, 10)

if __name__ == "__main__":
    unittest.main()
