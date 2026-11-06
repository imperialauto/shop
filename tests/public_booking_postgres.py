"""Public online booking against a disposable local PostgreSQL database. Made-up data only."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.engine import make_url

url = make_url(os.environ['TEST_DATABASE_URL'])
assert url.host in ('localhost', '127.0.0.1') and url.database == 'appointment_test'
os.environ['DATABASE_URL'] = str(url.render_as_string(hide_password=False))
os.environ['APP_READ_ONLY'] = 'false'
os.environ['SECRET_KEY'] = 'test-only-secret'
os.environ['PUBLIC_FORMS_ENABLED'] = 'true'
os.environ['SHOP_HOURS_JSON'] = '{"0":["10:00","18:00"],"1":["10:00","18:00"],"2":["10:00","18:00"],"3":["10:00","18:00"],"4":["10:00","18:00"]}'
os.environ.pop('PUBLIC_BOOKING_SERVICES', None)
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.database import Base, engine, SessionLocal
from app.models import Communication, Customer, RepairOrder, User, Vehicle
from app.appointment_model import Appointment, BookingSlot
from app.auth import hash_password
from app.routes import public_site as public_routes

MIGRATIONS = ('migrations/20261031_appointments.sql', 'migrations/20261102_live_overlay.sql', 'migrations/20261102_dispatcher.sql',
              'migrations/20261102_dispatch_draft.sql', 'migrations/20261102_dispatch_contact.sql', 'migrations/20261103_dispatch_call.sql')
MIGRATED = ('appointments', 'booking_slots', 'dispatch_items', 'dispatch_events', 'dispatch_policies', 'dispatch_audit', 'dispatch_outbound', 'dispatch_sync')


def weekday_at(hour: int, days: int = 7) -> datetime:
    start = (datetime.now(ZoneInfo('America/Phoenix')) + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)
    while start.weekday() > 4:
        start += timedelta(days=1)
    return start


class PublicBooking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name not in MIGRATED])
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            for path in MIGRATIONS:
                conn.execute(text(Path(path).read_text()))
        with SessionLocal() as db:
            db.add(User(username='fixture', password_hash=hash_password('fixture-pass'), role='owner'))
            db.commit()

    def setUp(self):
        with SessionLocal() as db:
            for model in (Appointment, BookingSlot, RepairOrder, Vehicle, Customer):
                db.query(model).delete()
            db.commit()
        public_routes.limiter.seen.clear()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def slot(self, start: datetime, minutes: int = 60, resource: str = 'bay-1', removed: bool = False) -> int:
        with SessionLocal() as db:
            item = BookingSlot(resource_id=resource, starts_at=start.astimezone(timezone.utc),
                               ends_at=(start + timedelta(minutes=minutes)).astimezone(timezone.utc),
                               removed_at=datetime.now(timezone.utc) if removed else None)
            db.add(item); db.commit()
            return item.id

    @staticmethod
    def form(slot_id, **changes):
        data = {'service': 'diagnostic', 'slot_id': str(slot_id), 'first_name': 'Testy', 'last_name': 'Sampleton',
                'phone': '602-555-0177', 'email': '', 'contact_method': 'text', 'year': '2016', 'make': 'Ford',
                'model': 'Focus', 'vin': '', 'mileage': '98000', 'notes': 'Made-up test: shudders from a stop.'}
        return {**data, **changes}

    def count(self, model):
        with SessionLocal() as db:
            return db.query(model).count()

    def test_booking_holds_the_time_for_staff_to_confirm(self):
        start = weekday_at(11)
        slot_id = self.slot(start)
        page = self.client.get('/site/book?service=diagnostic')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'value="{slot_id}"', page.text)

        response = self.client.post('/site/book', data=self.form(slot_id), follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        received = self.client.get('/site/book/received')
        self.assertIn('Diagnostic appointment', received.text)
        self.assertIn('Reference WEB-', received.text)

        with SessionLocal() as db:
            appointment = db.query(Appointment).one()
            self.assertEqual(appointment.status, 'tentative')
            self.assertEqual(appointment.location, 'shop')
            self.assertEqual(appointment.resource_id, 'bay-1')
            self.assertEqual(appointment.starts_at, start.astimezone(timezone.utc))
            self.assertEqual(appointment.buffer_after_minutes, 60)
            order = db.get(RepairOrder, appointment.repair_order_id)
            self.assertEqual(order.status, 'intake')
            self.assertTrue(order.concern.startswith('Online booking: Diagnostic appointment on '))
            self.assertIn('Made-up test: shudders from a stop.', order.concern)
            self.assertEqual(order.customer.last_name, 'Sampleton')
            self.assertEqual(db.query(Communication).count(), 0)

        # The time leaves the public list and the staff calendar shows the booking to confirm.
        self.assertNotIn(f'value="{slot_id}"', self.client.get('/site/book?service=diagnostic').text)
        self.client.post('/login', data={'username': 'fixture', 'password': 'fixture-pass'})
        listed = self.client.get('/appointments/').json()
        self.assertEqual([a['status'] for a in listed], ['tentative'])
        self.assertEqual(self.client.post(f"/appointments/{listed[0]['id']}/confirm").json()['status'], 'confirmed')

    def test_taken_removed_or_too_soon_times_are_refused(self):
        start = weekday_at(11)
        slot_id = self.slot(start)
        self.slot(weekday_at(16))  # another open time, so the form is offered again
        self.assertEqual(self.client.post('/site/book', data=self.form(slot_id), follow_redirects=False).status_code, 303)
        again = self.client.post('/site/book', data=self.form(slot_id, last_name='Other', phone='602-555-0188'))
        self.assertEqual(again.status_code, 409)
        self.assertIn('just taken', again.text)
        self.assertIn('Other', again.text)  # their details are kept for the next try

        removed = self.slot(weekday_at(14), removed=True)
        self.assertEqual(self.client.post('/site/book', data=self.form(removed)).status_code, 409)

        soon = self.slot(datetime.now(ZoneInfo('America/Phoenix')) + timedelta(minutes=30))
        self.assertNotIn(f'value="{soon}"', self.client.get('/site/book?service=diagnostic').text)
        self.assertEqual(self.client.post('/site/book', data=self.form(soon)).status_code, 409)

        self.assertEqual(self.client.post('/site/book', data=self.form(999999)).status_code, 409)
        self.assertEqual((self.count(Appointment), self.count(RepairOrder), self.count(Customer)), (1, 1, 1))

    def test_buffer_after_existing_booking_is_respected(self):
        first = weekday_at(11)
        booked = self.slot(first)
        inside_buffer = self.slot(first + timedelta(minutes=90))  # bay-1 is blocked until 1 PM
        other_bay = self.slot(first + timedelta(minutes=90), resource='bay-2')
        later = self.slot(first + timedelta(hours=3))
        self.assertEqual(self.client.post('/site/book', data=self.form(booked), follow_redirects=False).status_code, 303)
        page = self.client.get('/site/book?service=diagnostic').text
        self.assertIn(f'value="{other_bay}"', page)
        self.assertIn(f'value="{later}"', page)
        self.assertNotIn(f'value="{inside_buffer}"', page)
        self.assertEqual(self.client.post('/site/book', data=self.form(inside_buffer, phone='602-555-0199')).status_code, 409)
        self.assertEqual(self.client.post('/site/book', data=self.form(other_bay, phone='602-555-0199'), follow_redirects=False).status_code, 303)

    def test_only_approved_services_can_be_booked(self):
        slot_id = self.slot(weekday_at(11))
        with patch.dict(os.environ, {'PUBLIC_BOOKING_SERVICES': 'oil-service'}):
            self.assertNotIn('Diagnostic appointment', self.client.get('/site/book').text)
            response = self.client.post('/site/book', data=self.form(slot_id))
            self.assertEqual(response.status_code, 422)
            self.assertIn('Choose a service', response.text)
            self.assertEqual(self.client.post('/site/book', data=self.form(slot_id, service='oil-service'), follow_redirects=False).status_code, 303)
        self.assertEqual(self.count(Appointment), 1)

    def test_missing_details_book_nothing(self):
        slot_id = self.slot(weekday_at(11))
        for changes, message in [({'phone': '123'}, '10-digit'), ({'make': ''}, 'make and model'), ({'slot_id': ''}, 'Choose a time')]:
            response = self.client.post('/site/book', data={**self.form(slot_id), **changes})
            self.assertEqual(response.status_code, 422, message)
            self.assertIn(message, response.text)
        self.assertEqual((self.count(Appointment), self.count(RepairOrder)), (0, 0))

    def test_simultaneous_requests_for_one_time_only_one_wins(self):
        slot_id = self.slot(weekday_at(11))

        def attempt(n):
            with TestClient(app) as client:
                return client.post('/site/book', data=self.form(slot_id, phone=f'602-555-01{n:02d}', last_name=f'Racer{n}'),
                                   follow_redirects=False).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = sorted(pool.map(attempt, [1, 2]))
        self.assertEqual(codes, [303, 409])
        self.assertEqual(self.count(Appointment), 1)


if __name__ == '__main__':
    unittest.main()
