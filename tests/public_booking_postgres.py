"""Public online booking against a disposable local PostgreSQL database. Made-up data only."""
import json
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
os.environ['SERVICE_LEADS_ENABLED'] = 'true'
os.environ['SHOP_HOURS_JSON'] = '{"0":["10:00","18:00"],"1":["10:00","18:00"],"2":["10:00","18:00"],"3":["10:00","18:00"],"4":["10:00","18:00"]}'
os.environ.pop('PUBLIC_BOOKING_SERVICES', None)
os.environ['PUBLIC_SERVICE_CATALOG'] = json.dumps({
    'diagnostic': {'price': 10, 'minutes': 60}, 'oil-service': {'price': 20, 'minutes': 60},
    'pre-purchase': {'price': 30, 'price_type': 'starting', 'minutes': 90},
    'battery-charging': {'price': 40, 'minutes': 30, 'mobile': True}})
os.environ['PUBLIC_MOBILE_ZONES'] = json.dumps({'85000': {'fee': 5, 'travel_minutes': 30}})
from zoneinfo import ZoneInfo
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.database import Base, engine, SessionLocal
from app.models import Communication, Customer, RepairOrder, User, Vehicle
from app.appointment_model import Appointment, BookingSlot
from app.auth import hash_password
from app.lead_model import ServiceLead, ServiceLeadAudit
from app.routes import public_site as public_routes

MIGRATIONS = ('migrations/20261031_appointments.sql', 'migrations/20261102_live_overlay.sql', 'migrations/20261102_dispatcher.sql',
              'migrations/20261102_dispatch_draft.sql', 'migrations/20261102_dispatch_contact.sql', 'migrations/20261103_dispatch_call.sql',
              'migrations/20261106_service_leads.sql')
MIGRATED = ('appointments', 'booking_slots', 'dispatch_items', 'dispatch_events', 'dispatch_policies', 'dispatch_audit', 'dispatch_outbound', 'dispatch_sync', 'service_leads', 'service_lead_audit')


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
        # Run ONLY against the disposable CI database validated at module load.
        # Website requests create dispatch items that reference vehicles; an
        # ORM delete sequence can race with those app writes and leave a FK
        # reference behind. Clear all dependent fixture rows atomically rather
        # than weakening or dropping the real database constraints.
        assert url.host in ('localhost', '127.0.0.1') and url.database == 'appointment_test'
        with engine.begin() as conn:
            conn.execute(text(
                'TRUNCATE TABLE dispatch_items, dispatch_events, dispatch_audit, '
                'dispatch_outbound, appointments, booking_slots, repair_orders, '
                'vehicles, customers RESTART IDENTITY CASCADE'
            ))
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

    def test_booking_is_confirmed_on_screen_with_no_message(self):
        start = weekday_at(11)
        slot_id = self.slot(start)
        page = self.client.get('/site/book?service=diagnostic')
        self.assertEqual(page.status_code, 200)
        self.assertIn(f'value="{slot_id}"', page.text)

        response = self.client.post('/site/book', data=self.form(slot_id), follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        received = self.client.get('/site/book/received')
        self.assertIn('Diagnostic appointment', received.text)
        self.assertIn('booked', received.text)
        self.assertIn('/status/', received.text)
        self.assertIn('Reference WEB-', received.text)

        with SessionLocal() as db:
            appointment = db.query(Appointment).one()
            self.assertEqual(appointment.status, 'confirmed')
            lead = db.query(ServiceLead).one()
            self.assertEqual((lead.source, lead.status, lead.appointment_id),
                             ('website', 'booked', appointment.id))
            self.assertEqual(lead.closure_evidence_ref, f'appointment:{appointment.id}')
            self.assertEqual(float(appointment.booked_value), 10)
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
        self.assertEqual([a['status'] for a in listed], ['confirmed'])

    def test_estimate_tentative_confirmation_and_cancellation(self):
        data = {**self.form(0), 'where': 'shop', 'description': 'Synthetic intermittent brake noise.'}
        response = self.client.post('/site/estimate', data=data, follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        with SessionLocal() as db:
            website = db.query(ServiceLead).one()
            order_id = website.repair_order_id
            self.assertEqual(website.status, 'estimate_needed')
            # A Quo inquiry can concern another service even for the same customer and vehicle.
            db.add(ServiceLead(source='quo', external_thread_id='CNsynthetic', status='new',
                               customer_id=website.customer_id, vehicle_id=website.vehicle_id,
                               customer_phone='+16025550177'))
            db.commit()
        self.client.post('/login', data={'username': 'fixture', 'password': 'fixture-pass'})
        start = weekday_at(11)
        result = self.client.post('/appointments/', json={
            'repair_order_id': order_id, 'resource_id': 'bay-1',
            'starts_at': start.isoformat(), 'ends_at': (start + timedelta(hours=1)).isoformat(),
        })
        self.assertEqual(result.status_code, 201, result.text)
        appointment_id = result.json()['id']
        with SessionLocal() as db:
            self.assertEqual(db.query(ServiceLead).filter_by(source='website').one().status, 'estimate_needed')
        self.assertEqual(self.client.post(f'/appointments/{appointment_id}/confirm').status_code, 200)
        with SessionLocal() as db:
            lead = db.query(ServiceLead).filter_by(source='website').one()
            self.assertEqual((lead.status, lead.appointment_id), ('booked', appointment_id))
            self.assertEqual(db.query(ServiceLead).filter_by(source='quo').one().status, 'new')
        self.assertEqual(self.client.post(f'/appointments/{appointment_id}/cancel').status_code, 200)
        with SessionLocal() as db:
            lead = db.query(ServiceLead).filter_by(source='website').one()
            self.assertEqual((lead.status, lead.appointment_id, lead.resolved_at), ('scheduling', None, None))
            self.assertEqual([audit.action for audit in db.query(ServiceLeadAudit).order_by(ServiceLeadAudit.id)],
                             ['lead_opened', 'booked', 'booking_cancelled'])
            self.assertEqual(db.query(Communication).count(), 0)

    def test_website_leads_remain_feature_gated(self):
        with patch.dict(os.environ, {'SERVICE_LEADS_ENABLED': 'false'}):
            result = self.client.post('/site/book', data=self.form(self.slot(weekday_at(11))), follow_redirects=False)
        self.assertEqual(result.status_code, 303, result.text)
        self.assertEqual(self.count(Appointment), 1)
        self.assertEqual(self.count(ServiceLead), 0)

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

    def test_ninety_minute_service_reserves_ninety_plus_buffer(self):
        first = weekday_at(11)
        booked = self.slot(first)  # staff slots are 60 minutes, the service is 90
        inside = self.slot(first + timedelta(minutes=120))  # ends 12:30, buffer to 1:30
        after = self.slot(first + timedelta(minutes=150))
        response = self.client.post('/site/book', data=self.form(booked, service='pre-purchase'), follow_redirects=False)
        self.assertEqual(response.status_code, 303, response.text)
        with SessionLocal() as db:
            item = db.query(Appointment).one()
            self.assertEqual(item.ends_at - item.starts_at, timedelta(minutes=90))
            self.assertEqual(item.buffer_after_minutes, 60)
        page = self.client.get('/site/book?service=diagnostic').text
        self.assertNotIn(f'value="{inside}"', page)
        self.assertIn(f'value="{after}"', page)

    def test_long_service_is_not_offered_when_it_would_run_into_the_next_booking(self):
        first = weekday_at(11)
        short_gap = self.slot(first)
        self.slot(first + timedelta(minutes=120))
        self.assertEqual(self.client.post('/site/book', data=self.form(short_gap + 1), follow_redirects=False).status_code, 303)
        page = self.client.get('/site/book?service=pre-purchase').text
        self.assertNotIn(f'value="{short_gap}"', page)  # 90 minutes plus an hour runs into 1 PM
        self.assertEqual(self.client.post('/site/book', data=self.form(short_gap, service='pre-purchase', phone='602-555-0199')).status_code, 409)

    def test_service_and_buffer_must_end_by_closing(self):
        late = self.slot(weekday_at(16))  # closes at 6: 60 plus 60 fits, 90 plus 60 does not
        self.assertNotIn(f'value="{late}"', self.client.get('/site/book?service=pre-purchase').text)
        self.assertIn(f'value="{late}"', self.client.get('/site/book?service=diagnostic').text)
        self.assertEqual(self.client.post('/site/book', data=self.form(late, service='pre-purchase')).status_code, 409)

    def test_mobile_booking_adds_fee_and_travel_both_ways(self):
        first = weekday_at(12)
        slot_id = self.slot(first)
        form = self.form(slot_id, service='battery-charging', where='mobile', zip='85000', street='100 Test St')
        page = self.client.get('/site/book?service=battery-charging&where=mobile&zip=85000').text
        self.assertIn('$5.00', page)
        self.assertIn(f'value="{slot_id}"', page)
        self.assertEqual(self.client.post('/site/book', data=form, follow_redirects=False).status_code, 303, 'mobile')
        with SessionLocal() as db:
            item = db.query(Appointment).one()
            self.assertEqual((item.location, item.travel_before_minutes, item.buffer_after_minutes, float(item.booked_value)), ('mobile', 30, 30, 45))
            self.assertEqual(item.ends_at - item.starts_at, timedelta(minutes=30))
            self.assertIn('100 Test St', db.get(RepairOrder, item.repair_order_id).concern)

    def test_mobile_needs_a_served_zip_and_a_mobile_service(self):
        slot_id = self.slot(weekday_at(12))
        self.assertIn('send an estimate request', self.client.get('/site/book?service=battery-charging&where=mobile&zip=99999').text)
        for changes in ({'service': 'battery-charging', 'zip': '99999'}, {'service': 'diagnostic', 'zip': '85000'}):
            response = self.client.post('/site/book', data=self.form(slot_id, where='mobile', street='100 Test St', **{k: v for k, v in changes.items() if k != 'service'}, service=changes['service']))
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.count(Appointment), 0)

    def test_status_lookup_needs_reference_and_phone(self):
        slot_id = self.slot(weekday_at(11))
        self.client.post('/site/book', data=self.form(slot_id), follow_redirects=False)
        with SessionLocal() as db:
            order = db.query(RepairOrder).one()
            ref, token = f'WEB-{order.id}', order.public_token
        found = self.client.post('/site/status', data={'reference': ref, 'phone': '602-555-0177'}, follow_redirects=False)
        self.assertEqual(found.headers['location'], f'/status/{token}')
        self.assertEqual(self.client.get(found.headers['location']).status_code, 200)
        self.assertEqual(self.client.post('/site/status', data={'reference': ref, 'phone': '602-555-0100'}).status_code, 404)

    def test_service_without_a_final_price_cannot_be_booked(self):
        slot_id = self.slot(weekday_at(11))
        self.assertNotIn('A/C performance check', self.client.get('/site/book').text)
        self.assertIn('Price coming soon', self.client.get('/site/pricing').text)
        self.assertEqual(self.client.post('/site/book', data=self.form(slot_id, service='ac-check')).status_code, 422)

    def saturday_slot(self):
        start = weekday_at(12)
        while start.weekday() != 5:
            start += timedelta(days=1)
        return start

    def test_saturday_books_when_service_and_buffer_fit(self):
        hours = '{"0":["10:00","18:00"],"1":["10:00","18:00"],"2":["10:00","18:00"],"3":["10:00","18:00"],"4":["10:00","18:00"],"5":["12:00","17:00"]}'
        day = self.saturday_slot()
        with patch.dict(os.environ, {'SHOP_HOURS_JSON': hours}):
            fits = self.slot(day)
            late = self.slot(day.replace(hour=15))  # 90 minutes plus an hour ends at 5:30
            page = self.client.get('/site/book?service=pre-purchase').text
            self.assertIn(f'value="{fits}"', page)
            self.assertNotIn(f'value="{late}"', page)
            self.assertEqual(self.client.post('/site/book', data=self.form(fits, service='pre-purchase'), follow_redirects=False).status_code, 303)
            self.assertEqual(self.client.post('/site/book', data=self.form(late, service='pre-purchase', phone='602-555-0199')).status_code, 409)
        self.assertEqual(self.count(Appointment), 1)

    def test_preview_books_without_any_price(self):
        slot_id = self.slot(weekday_at(11))
        with patch.dict(os.environ, {'PUBLIC_PREVIEW_MODE': 'true', 'RAILWAY_ENVIRONMENT_NAME': 'preview', 'RAILWAY_PROJECT_ID': 'other'}):
            page = self.client.get('/site/book?service=ac-check').text
            self.assertIn('Price coming soon', page)
            self.assertNotIn('$', page.split('footer')[0])
            self.assertEqual(self.client.post('/site/book', data=self.form(slot_id, service='ac-check'), follow_redirects=False).status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(float(db.query(Appointment).one().booked_value), 0)

    def test_preview_switch_cannot_work_in_production(self):
        with patch.dict(os.environ, {'PUBLIC_PREVIEW_MODE': 'true', 'RAILWAY_ENVIRONMENT_NAME': 'production'}):
            self.assertNotIn('A/C performance check', self.client.get('/site/book').text)
        with patch.dict(os.environ, {'PUBLIC_PREVIEW_MODE': 'true', 'RAILWAY_ENVIRONMENT_NAME': 'other', 'RAILWAY_PROJECT_ID': '97a71288-74a7-4405-9789-b74fda7b8939'}):
            self.assertNotIn('A/C performance check', self.client.get('/site/book').text)

    def test_website_booking_and_request_reach_the_owner_queue(self):
        from app.dispatch_model import DispatchItem
        from app.routes import dispatch
        slot_id = self.slot(weekday_at(11))
        self.assertEqual(self.client.post('/site/book', data=self.form(slot_id), follow_redirects=False).status_code, 303)
        tow = {'service_line': 'towing', 'where': 'roadside', 'location': 'I-10 mile 140', 'description': 'Made-up: will not start on the shoulder.',
               'first_name': 'Tow', 'last_name': 'Visitor', 'phone': '602-555-0144', 'contact_method': 'text', 'make': 'Ford', 'model': 'Focus'}
        self.assertEqual(self.client.post('/site/estimate', data=tow, follow_redirects=False).status_code, 303)
        with SessionLocal() as db:
            booked, request = db.query(DispatchItem).order_by(DispatchItem.id).all()
            self.assertEqual((booked.status, booked.draft_reply), ('routine', ''))
            self.assertEqual((request.status, request.urgency), ('needs_owner', 'urgent'))
            text = dispatch._alert_text(db, request)
            self.assertIn('Nothing was sent to them', text)
            self.assertIn('URGENT', text)
            self.assertNotIn('YES', text)
            self.assertIn('Nothing needed', dispatch._alert_text(db, booked))

    def test_website_items_cannot_queue_a_dashboard_reply(self):
        from app.dispatch_model import DispatchItem, DispatchOutbound
        data = {**self.form(0), 'where': 'shop', 'description': 'Synthetic intermittent brake noise.'}
        self.assertEqual(self.client.post('/site/estimate', data=data, follow_redirects=False).status_code, 303)
        self.client.post('/login', data={'username': 'fixture', 'password': 'fixture-pass'})
        with SessionLocal() as db:
            item = db.query(DispatchItem).one()
            item_id, message_id = item.id, item.last_message_id
        response = self.client.post(f'/dispatch/{item_id}/decide', data={
            'decision': 'approve', 'expected_message_id': message_id, 'reply_text': 'Synthetic draft',
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn('must be contacted manually', response.text)
        self.assertEqual(self.count(DispatchOutbound), 0)

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
