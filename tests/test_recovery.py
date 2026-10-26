import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + str(Path(TEMP.name) / 'fixture.db')
os.environ['SECRET_KEY'] = 'fixture-only-session-secret'
os.environ['APP_READ_ONLY'] = 'true'
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from app.main import app
from app.database import Base, engine, SessionLocal
from app.models import User, Customer, Vehicle, RepairOrder, Invoice, LineItem, Payment
from app.auth import hash_password
from app.recovery import database_audit


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Explicit fixtures only, never a production connection.
        assert str(engine.url).startswith('sqlite:///') and TEMP.name in str(engine.url)
        cls.client = TestClient(app)

    def test_01_startup_has_no_ddl_or_seed_or_scheduler(self):
        with patch('app.main.init_db', side_effect=AssertionError('DDL called')), patch('app.main.start_scheduler', side_effect=AssertionError('scheduler called')):
            with TestClient(app) as client:
                self.assertEqual(client.get('/healthz').status_code, 200)
                self.assertEqual(client.get('/readyz').status_code, 503)
        self.assertEqual(inspect(engine).get_table_names(), [])
        # Set up a disposable baseline after proving startup is non-mutating.
        Base.metadata.create_all(engine)
        with SessionLocal() as db:
            user=User(username='fixture-owner',password_hash=hash_password('fixture-only-pass'),full_name='TEST Owner',role='admin')
            customer=Customer(first_name='TEST',last_name='Customer',phone='2025550100')
            db.add_all([user,customer]);db.flush()
            vehicle=Vehicle(customer_id=customer.id,year=2016,make='TEST',model='Vehicle');db.add(vehicle);db.flush()
            ro=RepairOrder(customer_id=customer.id,vehicle_id=vehicle.id,status='waiting_approval',public_token='fixture-token-123456789',concern='TEST ONLY');db.add(ro);db.flush()
            db.add(LineItem(repair_order_id=ro.id,description='TEST service',quantity=1,unit_price=100))
            invoice=Invoice(repair_order_id=ro.id,total=100,subtotal=100,status='sent');db.add(invoice);db.flush();db.add(Payment(invoice_id=invoice.id,amount=25,method='fixture'))
            db.commit()

    def test_02_existing_read_workflows_and_board_render(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/readyz').status_code,200)
            self.assertEqual(client.get('/dashboard',follow_redirects=False).status_code,302)
            self.assertEqual(client.post('/login',data={'username':'fixture-owner','password':'fixture-only-pass'},follow_redirects=False).status_code,302)
            for path in ['/dashboard','/customers/','/customers/1','/vehicles/1','/ro/','/ro/board','/ro/1','/invoices/','/invoices/1','/estimates/','/settings','/status/fixture-token-123456789','/sign/ro/fixture-token-123456789']:
                with self.subTest(path=path): self.assertEqual(client.get(path).status_code,200)
            self.assertIn('Waiting on customer signature',client.get('/ro/board').text)

    def test_03_recovery_blocks_production_mutations_and_outbound_actions(self):
        with TestClient(app) as client:
            for path in ['/customers/new','/vehicles/new','/ro/new','/ro/1/update-status','/ro/1/add-line-item','/invoices/1/add-payment','/sign/ro/fixture-token-123456789','/sign/invoice/1','/webhooks/openphone','/internal/messaging/communications','/estimates/generate']:
                with self.subTest(path=path): self.assertEqual(client.post(path,data={}).status_code,503)
        with SessionLocal() as db:
            self.assertEqual(db.query(Payment).count(),1)
            self.assertEqual(db.query(Invoice).first().status,'sent')
            self.assertIsNone(db.query(RepairOrder).first().signed_at)

    def test_04_invoice_document_requires_secret_token_and_signing_does_not_mark_paid(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/sign/invoice/1').status_code,404)
            self.assertEqual(client.get('/sign/invoice/1?token=wrong').status_code,404)
            self.assertEqual(client.get('/sign/invoice/1?token=fixture-token-123456789').status_code,200)
        from app.routes.sign import sign_invoice_submit
        import asyncio
        class FakeRequest:
            async def form(self): return {'signature_data':'data:image/png;base64,TEST', 'approved_by':'TEST ONLY'}
        with SessionLocal() as db:
            result=asyncio.run(sign_invoice_submit(FakeRequest(),1,'fixture-token-123456789',db))
            self.assertEqual(db.query(Invoice).first().status,'sent')
            self.assertEqual(db.query(Payment).count(),1)
        with SessionLocal() as db:
            invoice=db.query(Invoice).first();invoice.customer_signature=None;invoice.customer_approved_by=None;invoice.customer_signed_at=None;db.commit()

    def test_05_audit_is_read_only_aggregate_and_credential_free(self):
        before=Path(TEMP.name,'fixture.db').read_bytes()
        audit=database_audit()
        self.assertTrue(audit['connected'])
        self.assertEqual(audit['tables']['customers']['rows'],1)
        self.assertEqual(audit['missing_tables'],[])
        self.assertNotIn('fixture-only-pass',str(audit))
        self.assertNotIn('TEST Customer',str(audit))
        self.assertEqual(Path(TEMP.name,'fixture.db').read_bytes(),before)

if __name__=='__main__': unittest.main()
