"""Finance tests use invented data only: this repository and its CI logs are public."""
import os
import unittest
from contextlib import contextmanager
import fixture_environment  # noqa: F401
from datetime import date
os.environ['FINANCE_OWNER_USERNAMES'] = ''
from fastapi.testclient import TestClient
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app import finance_core as C, finance_engine as E, finance_rules as R
from app.finance_model import FinAudit, FinImportBatch, FinReview, FinStatement, FinTransaction

LINES = b"""account_last4,date,signed_amount,description,source_file
1111,2026-01-05,500.00,Zelle Payment From Test Customer Abc12345,s1.pdf
1111,2026-01-06,-100.00,Card Purchase 01/05 Autozone #1 Town AZ,s1.pdf
1111,2026-01-06,-25.00,Card Purchase 01/05 Autozone #1 Town AZ,s1.pdf
1111,2026-01-07,-25.00,Card Purchase 01/05 Autozone #1 Town AZ,s1.pdf
1111,2026-01-07,-25.00,Card Purchase 01/05 Autozone #1 Town AZ,s1.pdf
1111,2026-01-08,-200.00,Online Transfer To Chk ...2222 Transaction# 900001,s1.pdf
1111,2026-01-09,-20.00,Card Purchase Dutch Bros Town AZ,s1.pdf
2222,2026-01-08,200.00,Online Transfer From Chk ...1111 Transaction# 900001,s2.pdf
"""
ACCOUNTS = b"""account_last4,institution,name,designation,status
1111,Bank A,Shop checking,business,closed
2222,Bank B,Shop operating,business,active
3333,Bank A,Owner savings,personal,closed
"""
CONTROLS = b"""account_last4,period_start,period_end,beginning,deposits,withdrawals,ending,source_file
1111,2026-01-01,2026-01-31,100.00,500.00,395.00,205.00,s1.pdf
2222,2026-01-01,2026-01-31,0.00,200.00,0.00,200.00,s2.pdf
"""
OCR = """canonical_id,date,year,month,institution,account,direction,amount,signed_cashflow,description,proposed_category,category_confidence,classification_notes,source_pdf,source_page,statement_period,matching_occurrences,duplicate_screen,verification_status,issues
A1,2026-01-05,2026,2026-01,Chase,Business checking ending 1111,inflow,800.00,800.00,Zelle Payment From Test Customer Abc12345,x,x,x,b.pdf,1,Jan,1,primary,unverified,
B1,2025-03-03,2025,2025-03,Chase,Business checking ending 1111,inflow,1800.00,1800.00,Zelle Payment From Sample Fleet LLC Bacxyz123,x,x,x,b.pdf,2,Mar,1,primary,unverified,
B2,2025-03-03,2025,2025-03,Chase,Business checking ending 1111,inflow,4800.00,4800.00,Zelie Payment From Sample Fleet LLC Baoxyz123,x,x,x,b.pdf,9,Mar,1,primary,unverified,
B3,2025-03-04,2025,2025-03,Chase,Business checking ending 1111,outflow,12.00,-12.00,Card Purchase 03/03 Qt 1 Town AZ,x,x,x,b.pdf,2,Mar,2,primary,unverified,
B3,2025-03-04,2025,2025-03,Chase,Business checking ending 1111,outflow,12.00,-12.00,Card Purchase 03/03 Qt 1 Town AZ,x,x,x,b.pdf,9,Mar,2,dup,unverified,
B4,2025-03-05,2025,2025-03,Chase,Business checking ending 1111,outflow,5.00,-5.00,Card Purchase 03/04 Qt 1 Town AZ,x,x,x,b.pdf,3,Mar,2,primary,unverified,
B4,2025-03-05,2025,2025-03,Chase,Business checking ending 1111,outflow,5.00,-5.00,Card Purchase 03/04 Qt 1 Town AZ,x,x,x,b.pdf,3,Mar,2,primary,unverified,
""".encode()


class FinanceBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        db = SessionLocal()
        # 'owner' is the claimed owner account. 'advisor' and 'bookkeeper' are staff admins.
        db.add_all([User(username='owner', password_hash=hash_password('pw-owner'), role='owner'),
                    User(username='advisor', password_hash=hash_password('pw-advisor'), role='admin'),
                    User(username='bookkeeper', password_hash=hash_password('pw-bookkeeper'), role='admin')])
        db.commit()
        for raw, name in ((ACCOUNTS, 'a.csv'), (CONTROLS, 'c.csv'), (LINES, 'l.csv'), (OCR, 'o.csv')):
            E.import_csv(db, raw, name, 'owner')
        db.close()

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(engine)  # other modules assert an empty fixture database

    def setUp(self):
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()

    def client(self, user=None):
        c = TestClient(app, follow_redirects=False)
        if user:
            r = c.post('/login', data={'username': user, 'password': 'pw-' + user})
            self.assertEqual(r.status_code, 302)
        return c


@contextmanager
def writable():
    """Production runs in recovery (read-only) mode, which blocks finance writes.
    Write-path tests lift it only for the duration of the test."""
    before = app.state.read_only
    app.state.read_only = False
    try:
        yield
    finally:
        app.state.read_only = before


class Import(FinanceBase):
    def test_repeat_imports_add_nothing(self):
        before = self.db.query(FinTransaction).count()
        for raw in (LINES, OCR):
            b = E.import_csv(self.db, raw, 'again.csv', 'owner')
            self.assertEqual(b.rows_added, 0)
        self.assertEqual(self.db.query(FinTransaction).count(), before)
        self.assertEqual(self.db.query(FinStatement).count(), 2)

    def test_real_repeats_in_one_file_survive(self):
        rows = self.db.query(FinTransaction).filter_by(source_type='statement', amount_cents=-2500, posted_date=date(2026, 1, 7)).all()
        self.assertEqual(len(rows), 2)

    def test_full_account_numbers_are_not_stored(self):
        from app.finance_model import FinAccount
        self.assertTrue(all(len(a.last4) == 4 for a in self.db.query(FinAccount)))

    def test_account_list_sets_designation(self):
        from app.finance_model import FinAccount
        self.assertEqual(self.db.query(FinAccount).filter_by(last4='3333').one().designation, 'personal')

    def test_bad_file_rejected(self):
        with self.assertRaises(ValueError):
            E.import_csv(self.db, b'a,b\n1,2\n', 'bad.csv', 'owner')
        self.db.rollback()


class Reconcile(FinanceBase):
    def test_statement_ties_and_flags_variance(self):
        status = {s['statement'].account_id: s for s in E.statement_status(self.db)}
        self.assertTrue(all(s['ties'] for s in status.values()))
        self.assertEqual(C.money(next(iter(status.values()))['variance']), '$0.00')

    def test_ocr_inside_tied_period_is_superseded_not_deleted(self):
        row = self.db.query(FinTransaction).filter_by(source_txn_id='A1').one()
        self.assertEqual(row.duplicate_status, 'superseded')
        self.assertEqual(row.amount_cents, 80000)

    def test_misread_digit_duplicate_keeps_lower_and_flags(self):
        rows = {r.source_txn_id: r for r in self.db.query(FinTransaction).filter(FinTransaction.source_txn_id.in_(['B1', 'B2']))}
        self.assertEqual(rows['B2'].duplicate_status, 'amount_conflict')
        self.assertEqual(rows['B1'].duplicate_status, 'unique')
        self.assertTrue(rows['B1'].needs_review)

    def test_scan_copy_is_duplicate_but_same_page_repeat_is_real(self):
        b3 = self.db.query(FinTransaction).filter_by(source_txn_id='B3').all()
        b4 = self.db.query(FinTransaction).filter_by(source_txn_id='B4').all()
        self.assertEqual(sorted(r.duplicate_status for r in b3), ['confirmed_duplicate', 'unique'])
        self.assertEqual({r.duplicate_status for r in b4}, {'unique'})

    def test_transfer_matched_and_not_revenue(self):
        rows = self.db.query(FinTransaction).filter_by(report_group=R.TRANSFER).all()
        self.assertEqual({r.transfer_status for r in rows}, {'matched'})
        self.assertEqual(C.pnl(E.reportable(self.db).all())['revenue'], 50000)

    def test_pnl_separates_personal_and_tracks_margin(self):
        p = C.pnl(E.reportable(self.db).all())
        self.assertEqual((p['cogs'], p['personal'], p['gross']), (17500, 2000, 32500))

    def test_missing_months_are_missing_not_zero(self):
        cov = E.coverage(self.db)
        self.assertEqual(cov['grid']['2023-01']['1111'], 'missing')
        self.assertEqual(cov['grid']['2025-03']['1111'], 'ocr')
        self.assertEqual(cov['grid']['2026-01']['1111'], 'tied')

    def test_closed_month_needs_every_business_account(self):
        self.assertEqual(E.closed_months(self.db), ['2026-01'])


class Classify(unittest.TestCase):
    def test_refund_offsets_cost(self):
        self.assertEqual(R.classify('Card Purchase Return Autozone #1', 1000).group, R.COGS)

    def test_cash_deposit_is_not_revenue(self):
        self.assertEqual(R.classify('ATM Cash Deposit 1 Main St', 50000).group, R.UNCONFIRMED)

    def test_large_tool_needs_review(self):
        self.assertTrue(R.classify('Harbor Freight Tools', -300000).needs_review)

    def test_private_rule_wins(self):
        rule = type('X', (), dict(pattern='sample llc', report_group=R.REVENUE, category='Fleet and contract revenue', designation='business', division='repair', needs_review=False, note=''))
        self.assertEqual(R.classify('Zelle Payment From Sample LLC', 100, [rule]).category, 'Fleet and contract revenue')

    def test_digit_misread(self):
        self.assertTrue(C._digit_misread(180000, 480000))
        self.assertFalse(C._digit_misread(180000, 250000))


class Security(FinanceBase):
    PATHS = ['/finance/?tab=' + t for t in ('overview', 'ledger', 'recon', 'reports', 'accounts', 'investor', 'import')] + ['/finance/export/ledger.csv', '/finance/export/pnl.csv', '/finance/export/investor.pdf']

    def test_anonymous_cannot_read(self):
        c = self.client()
        for p in self.PATHS:
            self.assertIn(c.get(p).status_code, (302, 401), p)

    def test_admin_staff_are_refused_and_logged(self):
        c = self.client('advisor')
        for p in self.PATHS:
            self.assertEqual(c.get(p).status_code, 403, p)
        with writable():
            self.assertEqual(c.post('/finance/import', files={'file': ('x.csv', LINES)}).status_code, 403)
            self.assertEqual(c.post('/finance/txn/1/duplicate', data={'action': 'exclude'}).status_code, 403)
        self.assertGreater(self.db.query(FinAudit).filter_by(actor='advisor', allowed=False).count(), 0)

    def test_admin_staff_see_shop_payments_but_no_ledger_tabs(self):
        page = self.client('advisor').get('/finance/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Financial Command Center', page.text)
        self.assertNotIn('tab=ledger', page.text)
        self.assertNotIn('tab=import', page.text)

    def test_admin_role_alone_is_never_enough(self):
        c = self.client('bookkeeper')
        self.assertEqual(c.get('/finance/?tab=overview').status_code, 403)

    def test_listed_login_is_allowed(self):
        os.environ['FINANCE_OWNER_USERNAMES'] = 'bookkeeper'
        try:
            self.assertEqual(self.client('bookkeeper').get('/finance/?tab=overview').status_code, 200)
        finally:
            os.environ['FINANCE_OWNER_USERNAMES'] = ''


class Screens(FinanceBase):
    def test_every_tab_and_export_works_for_owner(self):
        c = self.client('owner')
        for p in Security.PATHS + ['/finance/', '/finance/?tab=shop']:
            self.assertEqual(c.get(p).status_code, 200, p)
        self.assertIn('/finance/', c.get('/dashboard').text)
        self.assertIn('tab=recon', c.get('/finance/').text)
        self.assertTrue(c.get('/finance/export/investor.pdf').content.startswith(b'%PDF'))

    def test_review_is_logged_survives_rebuild_and_keeps_source(self):
        c = self.client('owner')
        row = self.db.query(FinTransaction).filter_by(source_txn_id='B1').one()
        source = (row.amount_cents, row.description)
        with writable():
            r = c.post(f'/finance/txn/{row.id}/review', data={'report_group': 'owner_contribution', 'category': 'Owner contribution', 'designation': 'business', 'division': 'unassigned', 'note': 'mine'})
        self.assertEqual(r.status_code, 303)
        E.rebuild(self.db); self.db.commit()
        self.db.expire_all()
        row = self.db.get(FinTransaction, row.id)
        self.assertEqual(row.report_group, 'owner_contribution')
        self.assertEqual((row.amount_cents, row.description), source)
        self.assertEqual(self.db.query(FinReview).filter_by(transaction_id=row.id).count(), 1)

    def test_csv_upload_is_paused_in_read_only_mode(self):
        c = self.client('owner')
        before = self.db.query(FinImportBatch).count()
        self.assertTrue(app.state.read_only)
        r = c.post('/finance/import', files={'file': ('l.csv', LINES, 'text/csv')})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.db.query(FinImportBatch).count(), before)

    def test_csv_upload_when_writes_are_allowed(self):
        c = self.client('owner')
        with writable():
            r = c.post('/finance/import', files={'file': ('l.csv', LINES, 'text/csv')})
        self.assertEqual(r.status_code, 303)

    def test_tables_ready_after_migration(self):
        self.assertTrue(E.tables_ready(self.db))

    def test_export_blocks_formula_injection(self):
        from app.routes.finance import safe
        self.assertTrue(safe('=HYPERLINK(1)').startswith("'"))

    def test_amortize(self):
        a = C.amortize(4000000, 24, 20)
        self.assertEqual((a['payment'], a['total']), (200000, 4800000))
        self.assertAlmostEqual(a['apr'], 18.2, delta=0.5)
