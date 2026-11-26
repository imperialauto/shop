"""Calendar tab: each visit says who, what vehicle and what work; the job's Schedule button
opens a day-then-time picker; booking and Confirm keep their old behavior. Made-up data only."""
import json
import os
import unittest
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from test_agent_api import _Setup

from app import business_settings as store
from app import calendar_view
from app.appointment_model import Appointment
from app.appointments import PHOENIX, AppointmentWindow
from app.dispatch_model import DispatchOutbound
from app.imperial_task_model import ImperialTask
from app.models import Communication, Customer, LineItem, RepairOrder, Vehicle

ALL_WEEK = json.dumps({str(d): ["08:00", "18:00"] for d in range(7)})


def phoenix_tomorrow_at(hour: int) -> datetime:
    tomorrow = datetime.now(PHOENIX).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(hour), PHOENIX)


class _Calendar(_Setup):
    def setUp(self):
        super().setUp()
        self.hours_env = patch.dict(os.environ, {"SHOP_HOURS_JSON": ALL_WEEK, "SHOP_BUFFER_MINUTES": "60",
                                                 "IMPERIAL_TASKS_ENABLED": "true"})
        self.hours_env.start()
        store.clear_cache()
        with self.Session() as db:
            named = Customer(first_name="Testa", last_name="Example", phone="6025550199")
            unknown = Customer(first_name="Unknown", last_name="(0198)", phone="6025550198")
            db.add_all([named, unknown]); db.flush()
            car = Vehicle(customer_id=named.id, year=2004, make="Testmake", model="Fixture")
            blank = Vehicle(customer_id=unknown.id)
            db.add_all([car, blank]); db.flush()
            job = RepairOrder(customer_id=named.id, vehicle_id=car.id, concern="Grinding noise from the front when braking",
                              public_token="cal-a")
            other = RepairOrder(customer_id=unknown.id, vehicle_id=blank.id, concern="Check engine light", public_token="cal-b")
            db.add_all([job, other]); db.flush()
            db.add(LineItem(repair_order_id=job.id, description="Replace front brake pads", item_type="labor",
                            quantity=1, unit_price=100))
            db.commit()
            self.ids = SimpleNamespace(job=job.id, other=other.id)

    def tearDown(self):
        self.hours_env.stop()
        store.clear_cache()
        super().tearDown()

    def add(self, ro_id, start, status="tentative", hours=1, resource="bay 1"):
        with self.Session() as db:
            item = Appointment(repair_order_id=ro_id, resource_id=resource, starts_at=start.astimezone(timezone.utc),
                               ends_at=(start + timedelta(hours=hours)).astimezone(timezone.utc), status=status,
                               location="shop", buffer_after_minutes=60)
            db.add(item); db.commit()
            return item.id

    def postgres_shim(self):
        """The booking API insists on PostgreSQL's advisory lock; stand it in on SQLite."""
        raw = self.engine.raw_connection()
        raw.driver_connection.create_function("hashtext", 1, lambda value: 0)
        raw.driver_connection.create_function("pg_advisory_xact_lock", 1, lambda value: None)
        raw.close()
        return patch.object(self.engine.dialect, "name", "postgresql")


class SlotLabelTests(_Calendar):
    def test_each_visit_shows_customer_vehicle_work_and_time(self):
        start = phoenix_tomorrow_at(10)
        appt_id = self.add(self.ids.job, start)
        self.add(self.ids.other, phoenix_tomorrow_at(14), status="confirmed")
        page = self.sign_in().get("/appointments/calendar")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn(f'id="appt-{appt_id}"', html)
        self.assertIn("Testa Example", html)
        self.assertIn("2004 Testmake Fixture", html)
        self.assertIn("Replace front brake pads", html)       # what: the job's labor line
        self.assertIn("10:00 AM – 11:00 AM", html)
        self.assertIn("Customer not named yet", html)          # "Unknown (0198)" is never shown as a name
        self.assertIn("Vehicle details not given yet", html)
        self.assertIn("Check engine light", html)              # no lines yet: the concern
        # Tapping opens the details: phone, concern, the job
        self.assertIn('href="tel:+16025550199">(602) 555-0199</a>', html)
        self.assertIn("Grinding noise from the front when braking", html)
        self.assertIn(f'href="/ro/{self.ids.job}">Open job #{self.ids.job}</a>', html)
        # Plain-words key, and what Confirm does
        self.assertIn("hasn't agreed to it yet", html)
        self.assertIn("It doesn't text the customer.", html)
        self.assertIn(f'data-appt="{appt_id}" data-action="confirm"', html)

    def test_label_helpers(self):
        self.assertEqual(calendar_view.phone_text("+1 602 555 0199"), "(602) 555-0199")
        self.assertEqual(calendar_view.phone_link("602-555-0199"), "tel:+16025550199")
        self.assertEqual(calendar_view.phone_link("12"), "")
        self.assertEqual(calendar_view.customer_name(None), "Customer not named yet")

    def test_past_open_visit_is_listed_to_close_out(self):
        with self.Session() as db:
            db.add(Appointment(repair_order_id=self.ids.job, resource_id="bay 1",
                               starts_at=datetime.now(timezone.utc) - timedelta(hours=5),
                               ends_at=datetime.now(timezone.utc) - timedelta(hours=4), status="confirmed"))
            db.commit()
        html = self.sign_in().get("/appointments/calendar").text
        self.assertIn("Past visits to close out", html)
        self.assertIn('data-action="missed"', html)
        self.assertIn('data-action="complete"', html)

    def test_tech_sees_details_but_no_change_buttons(self):
        from app.auth import hash_password
        from app.models import User
        from test_agent_api import PASSWORD
        with self.Session() as db:
            db.add(User(id=3, username="tech", password_hash=hash_password(PASSWORD), role="tech")); db.commit()
        self.add(self.ids.job, phoenix_tomorrow_at(10))
        page = self.sign_in("tech").get(f"/appointments/calendar?ro={self.ids.job}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Testa Example", page.text)
        self.assertNotIn("data-action=", page.text)
        self.assertNotIn("Book a time for job", page.text)
        self.assertNotIn('id="appointment-form"', page.text)


class PickerTests(_Calendar):
    def test_schedule_button_opens_day_then_time_picker(self):
        day = phoenix_tomorrow_at(0).date().isoformat()
        page = self.sign_in().get(f"/appointments/calendar?ro={self.ids.job}&day={day}")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn(f"Book a time for job #{self.ids.job}", html)
        self.assertIn("Testa Example", html)
        self.assertIn("1. Pick a day", html)
        self.assertIn(f'data-start="{phoenix_tomorrow_at(8).isoformat()}"', html)
        self.assertIn(f'data-start="{phoenix_tomorrow_at(17).isoformat()}"', html)  # last one-hour start
        self.assertNotIn(f'data-start="{phoenix_tomorrow_at(18).isoformat()}"', html)
        # Tapping a time comes first; a typed exact time is the fallback, and the bay is never typed
        self.assertLess(html.index("2. Tap a time"), html.index("Or type an exact time"))
        self.assertIn(f'name="repair_order_id" value="{self.ids.job}"', html)
        self.assertIn('type="hidden" name="resource_id"', html)
        self.assertNotIn('placeholder="bay-1"', html)

    def test_taken_times_and_buffer_are_not_offered(self):
        self.add(self.ids.other, phoenix_tomorrow_at(10))  # blocks 10-11, plus an hour's buffer to noon
        day = phoenix_tomorrow_at(0).date().isoformat()
        html = self.sign_in().get(f"/appointments/calendar?ro={self.ids.job}&day={day}").text
        for hour in (10, 11):
            self.assertNotIn(f'data-start="{phoenix_tomorrow_at(hour).isoformat()}"', html)
        self.assertIn(f'data-start="{phoenix_tomorrow_at(8).isoformat()}"', html)  # 8-9 + buffer ends right at 10
        self.assertNotIn(f'data-start="{(phoenix_tomorrow_at(8) + timedelta(minutes=30)).isoformat()}"', html)
        self.assertIn(f'data-start="{phoenix_tomorrow_at(12).isoformat()}"', html)
        self.assertIn("Already booked that day", html)

    def test_free_starts_respects_hours_buffer_and_now(self):
        day = phoenix_tomorrow_at(0).date()
        hours = {day.weekday(): (time(9), time(12))}
        busy = [AppointmentWindow(phoenix_tomorrow_at(10), phoenix_tomorrow_at(11), "bay 1", 0, 60)]
        starts = calendar_view.free_starts(day, hours, busy, "bay 1", 60, 0, 0, datetime.now(timezone.utc))
        self.assertEqual([s.hour for s in starts if s.minute == 0], [9])
        other_bay = calendar_view.free_starts(day, hours, busy, "bay 2", 60, 0, 0, datetime.now(timezone.utc))
        self.assertEqual(len(other_bay), 5)  # 9:00 .. 11:00 every half hour

    def test_unknown_job_and_missing_hours_explain_themselves(self):
        client = self.sign_in()
        page = client.get("/appointments/calendar?ro=99999")
        self.assertEqual(page.status_code, 200)
        self.assertIn("no job #99999", page.text)
        with patch.dict(os.environ, {"SHOP_HOURS_JSON": ""}):
            store.clear_cache()
            page = client.get(f"/appointments/calendar?ro={self.ids.job}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("so no times can be booked", page.text)


class BookingAndConfirmTests(_Calendar):
    def test_picking_a_time_books_a_tentative_visit_for_the_job(self):
        start, end = phoenix_tomorrow_at(13), phoenix_tomorrow_at(14)
        client = self.sign_in()
        with self.postgres_shim():
            made = client.post("/appointments/", json={"repair_order_id": self.ids.job, "resource_id": "bay 1",
                                                       "starts_at": start.isoformat(), "ends_at": end.isoformat(),
                                                       "location": "shop", "travel_minutes": 0})
            self.assertEqual(made.status_code, 201, made.text)
            again = client.post("/appointments/", json={"repair_order_id": self.ids.other, "resource_id": "bay 1",
                                                        "starts_at": start.isoformat(), "ends_at": end.isoformat(),
                                                        "location": "shop", "travel_minutes": 0})
            self.assertEqual(again.status_code, 409)
        body = made.json()
        self.assertEqual(body["status"], "tentative")
        self.assertEqual(body["repair_order_id"], self.ids.job)
        with self.Session() as db:
            item = db.get(Appointment, body["id"])
            self.assertEqual(item.resource_id, "bay 1")
            self.assertEqual(item.buffer_after_minutes, 60)
            self.assertEqual(calendar_view.aware(item.starts_at), start.astimezone(timezone.utc))
        page = client.get(f"/appointments/calendar?booked={body['id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"for job #{self.ids.job} as tentative. Nothing was texted to the customer.", page.text)

    def test_confirm_marks_confirmed_adds_reminder_and_texts_nobody(self):
        appt_id = self.add(self.ids.job, phoenix_tomorrow_at(15))
        client = self.sign_in()
        with self.postgres_shim():
            done = client.post(f"/appointments/{appt_id}/confirm")
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual(done.json(), {"id": appt_id, "status": "confirmed"})
            self.assertEqual(client.post(f"/appointments/{appt_id}/confirm").status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.get(Appointment, appt_id).status, "confirmed")
            self.assertEqual(db.query(ImperialTask).filter_by(source="appointment", external_id=str(appt_id)).count(), 1)
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
            self.assertEqual(db.query(Communication).count(), 0)
        listed = client.get("/appointments/").json()
        self.assertEqual({"id", "repair_order_id", "resource_id", "starts_at", "ends_at", "status", "booked_value",
                          "location", "missed", "travel_before_minutes", "buffer_after_minutes"}, set(listed[0]))
        html = client.get("/appointments/calendar").text
        self.assertIn("pill-confirmed", html)
        self.assertIn(f'data-appt="{appt_id}" data-action="complete"', html)


if __name__ == "__main__":
    unittest.main()
