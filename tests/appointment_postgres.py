"""Run in a separate process against a disposable local PostgreSQL database only."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy.engine import make_url

url = make_url(os.environ['TEST_DATABASE_URL'])
assert url.host in ('localhost', '127.0.0.1') and url.database == 'appointment_test'
os.environ['DATABASE_URL'] = str(url.render_as_string(hide_password=False))
os.environ['APP_READ_ONLY'] = 'false'
os.environ['SECRET_KEY'] = 'test-only-secret'
os.environ['SHOP_HOURS_JSON'] = '{"0":["10:00","18:00"],"1":["10:00","18:00"],"2":["10:00","18:00"],"3":["10:00","18:00"],"4":["10:00","18:00"]}'
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.main import app
from app.database import Base, engine, SessionLocal
from app.models import Customer, Vehicle, RepairOrder, User
from app.appointment_model import Appointment
from app.auth import hash_password
from zoneinfo import ZoneInfo

class PostgreSQLWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Recreate only the explicitly named disposable fixture database tables.
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables if t.name != 'appointments'])
        migration = Path('migrations/20261031_appointments.sql').read_text()
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            conn.execute(text(migration))
            conn.execute(text(migration))  # additive migration is repeatable
        with SessionLocal() as db:
            owner = User(username='fixture', password_hash=hash_password('fixture-pass'), role='admin')
            customer = Customer(first_name='TEST', last_name='ONLY')
            db.add_all([owner, customer]); db.flush()
            vehicle = Vehicle(customer_id=customer.id, make='TEST', model='ONLY')
            db.add(vehicle); db.flush()
            ro = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, public_token='fixture-only')
            db.add(ro); db.commit()
            cls.ro_id = ro.id

    def setUp(self):
        with SessionLocal() as db:
            db.query(Appointment).delete(); db.commit()
        self.client = TestClient(app)
        self.client.post('/login', data={'username':'fixture', 'password':'fixture-pass'})
        start = (datetime.now(ZoneInfo('America/Phoenix')) + timedelta(days=7)).replace(hour=11,minute=0,second=0,microsecond=0)
        while start.weekday() > 4: start += timedelta(days=1)
        self.payload = dict(repair_order_id=self.ro_id, resource_id='bay-1', starts_at=start.isoformat(), ends_at=(start+timedelta(hours=1)).isoformat())

    def tearDown(self):
        self.client.close()

    def book(self, **changes):
        return self.client.post('/appointments/', json={**self.payload, **changes})

    def test_calendar_and_complete_lifecycle(self):
        self.assertEqual(self.client.get('/appointments/calendar').status_code, 200)
        created = self.book(); self.assertEqual(created.status_code, 201, created.text)
        ident = created.json()['id']
        self.assertEqual(self.client.post(f'/appointments/{ident}/complete').status_code, 409)
        self.assertEqual(self.client.post(f'/appointments/{ident}/confirm').json()['status'], 'confirmed')
        self.assertEqual(self.client.post(f'/appointments/{ident}/complete').json()['status'], 'completed')
        self.assertEqual(self.client.post(f'/appointments/{ident}/cancel').status_code, 409)
        self.assertEqual(self.client.get('/appointments/').json()[0]['status'], 'completed')
        self.assertEqual(self.book().status_code, 201)

    def test_cancellation_releases_resource(self):
        ident = self.book().json()['id']
        self.assertEqual(self.client.post(f'/appointments/{ident}/cancel').json()['status'], 'cancelled')
        self.assertEqual(self.client.post(f'/appointments/{ident}/confirm').status_code, 409)
        self.assertEqual(self.book().status_code, 201)

    def test_conflict_normalization_back_to_back_and_other_bay(self):
        self.assertEqual(self.book().status_code, 201)
        self.assertEqual(self.book(resource_id=' BAY-1 ').status_code, 409)
        self.assertEqual(self.book(resource_id='bay-2').status_code, 201)
        end = datetime.fromisoformat(self.payload['ends_at'])
        self.assertEqual(self.book(starts_at=end.isoformat(), ends_at=(end+timedelta(hours=1)).isoformat()).status_code, 201)

    def test_simultaneous_bookings_only_one_wins(self):
        def attempt(_):
            with TestClient(app) as client:
                client.post('/login', data={'username':'fixture','password':'fixture-pass'})
                return client.post('/appointments/', json=self.payload).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(attempt, range(2))), [201,409])

    def test_authentication_and_validation(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/appointments/').status_code,401)
            self.assertEqual(client.post('/appointments/',json=self.payload).status_code,401)
        self.assertEqual(self.book(resource_id='   ').status_code,422)
        self.assertEqual(self.book(repair_order_id=999999).status_code,404)
        self.assertEqual(self.client.post('/appointments/999999/confirm').status_code,404)
        self.assertEqual(self.book(starts_at='2000-01-03T11:00:00-07:00', ends_at='2000-01-03T12:00:00-07:00').status_code,422)
        self.assertEqual(self.book(starts_at=self.payload['starts_at'][:19], ends_at=self.payload['ends_at'][:19]).status_code,422)

if __name__ == '__main__': unittest.main()
