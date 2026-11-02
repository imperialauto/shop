import os
from fixture_environment import TEMP
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from fastapi import HTTPException
from app.routes.appointments import configured_hours, AppointmentInput
from app.appointments import AppointmentWindow, AppointmentValidationError, validate_slot

class BookingConfigurationTests(unittest.TestCase):
    def test_hours_missing_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(configured_hours(), {})
    def test_invalid_json_rejected(self):
        with patch.dict(os.environ, {"SHOP_HOURS_JSON": "{bad"}):
            with self.assertRaises(HTTPException) as ctx:
                configured_hours()
            self.assertEqual(ctx.exception.status_code, 503)
    def test_invalid_interval_rejected(self):
        with patch.dict(os.environ, {"SHOP_HOURS_JSON": '{"0":["18:00","08:00"]}'}):
            with self.assertRaises(HTTPException):
                configured_hours()
    def test_explicit_phoenix_hours(self):
        with patch.dict(os.environ, {"SHOP_HOURS_JSON": '{"0":["08:00","18:00"],"1":["09:30","17:00"]}'}):
            hours=configured_hours()
            self.assertEqual(hours[0][0].hour, 8)
            self.assertEqual(hours[1][0].minute, 30)
    def test_naive_times_rejected(self):
        with self.assertRaises(AppointmentValidationError):
            AppointmentWindow(datetime(2026,11,2,10),datetime(2026,11,2,11),"bay-1")
    def test_booking_input_requires_ro(self):
        with self.assertRaises(Exception):
            AppointmentInput(repair_order_id=0,resource_id="bay-1",starts_at=datetime.now(timezone.utc)+timedelta(days=1),ends_at=datetime.now(timezone.utc)+timedelta(days=1,hours=1))
