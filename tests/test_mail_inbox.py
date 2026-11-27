"""The Email page: sync, sorting, alerts, replies and access. Made-up data and a pretend mailbox only."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import mail_fake
from app import mail_gmail, mail_parse, mail_sort, mail_sync
from app import business_settings as store
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchAudit, DispatchItem
from app.mail_model import MailAccount, MailAttachment, MailMessage, MailThread
from app.main import app
from app.models import Communication, Customer, RepairOrder, User, Vehicle
from app.permissions import allowed

PASSWORD = "fixture-password-1"
SHOP = "shop@example-auto.test"


class SortTests(unittest.TestCase):
    def test_uhaul_offer_is_urgent(self):
        s = mail_sort.sort_message("noreply@roadside.uhaul.com", "New Roadside Service Request #4471")
        self.assertEqual((s.category, s.urgent), ("uhaul", True))

    def test_uhaul_newsletter_is_quiet(self):
        s = mail_sort.sort_message("news@uhaul.com", "Your November dealer update")
        self.assertEqual((s.category, s.urgent), ("uhaul", False))

    def test_web_form_is_a_lead(self):
        s = mail_sort.sort_message("form-submission@squarespace.info", "Form Submission - Get a quote")
        self.assertEqual((s.category, s.urgent), ("leads", True))

    def test_review_notice_is_not_a_lead(self):
        s = mail_sort.sort_message("businessprofile-noreply@google.com", "You have a new review")
        self.assertFalse(s.urgent)

    def test_known_customer_is_a_lead(self):
        s = mail_sort.sort_message("pat@example.test", "question about my car", known_customer=True)
        self.assertEqual((s.category, s.urgent), ("leads", True))

    def test_past_due_bill_is_urgent_and_plain_bill_quiet(self):
        self.assertEqual(mail_sort.sort_message("billing@spectrum.com", "Your account is past due").urgent, True)
        plain = mail_sort.sort_message("billing@spectrum.com", "Your statement is ready")
        self.assertEqual((plain.category, plain.urgent), ("bills", False))
        self.assertTrue(mail_sort.sort_message("x@example.test", "Payment failed for your subscription").urgent)

    def test_vendor_and_other(self):
        self.assertEqual(mail_sort.sort_message("orders@autozonepro.com", "Order 123 shipped").category, "vendors")
        self.assertEqual(mail_sort.sort_message("friend@example.test", "lunch?").category, "other")

    def test_outgoing_never_alerts(self):
        self.assertFalse(mail_sort.sort_message("noreply@roadside.uhaul.com", "Roadside offer", outgoing=True).urgent)


class ParseTests(unittest.TestCase):
    def test_parse_reads_headers_bodies_and_files(self):
        raw = mail_fake.raw_message("m1", "t1", "Pat Lee <Pat@Example.test>", "Brakes", "Hi there",
                                    html="<p>Hi <b>there</b></p>", reply_to="pat.home@example.test",
                                    attachments=[("photo.jpg", "image/jpeg", b"abc")])
        data = mail_parse.parse_message(raw)
        self.assertEqual(data["from_address"], "pat@example.test")
        self.assertEqual(data["from_name"], "Pat Lee")
        self.assertEqual(data["body_text"], "Hi there")
        self.assertIn("<b>there</b>", data["body_html"])
        self.assertEqual(data["attachments"][0]["filename"], "photo.jpg")
        self.assertEqual(data["sent_at"].tzinfo is not None, True)

    def test_html_only_mail_becomes_text_with_links(self):
        text = mail_parse.html_to_text('<style>p{}</style><p>Pay <a href="https://example.test/pay">here</a></p><p>Thanks</p>')
        self.assertIn("Pay here (https://example.test/pay)", text)
        self.assertIn("Thanks", text)
        self.assertNotIn("p{}", text)

    def test_reply_goes_to_reply_to(self):
        msg = MailMessage(from_address="form-submission@squarespace.info", reply_to="Jo <jo@example.test>")
        self.assertEqual(mail_parse.reply_target(msg), "jo@example.test")
        self.assertTrue(mail_parse.is_noreply("noreply@roadside.uhaul.com"))
        self.assertFalse(mail_parse.is_noreply("jo@example.test"))

    def test_build_message_threads_reply(self):
        raw = mail_parse.build_message(SHOP, "Imperial Auto Care", "jo@example.test", "Re: Brakes", "Sure\n",
                                       in_reply_to="<abc@x>", references="<first@x>",
                                       files=[("a.pdf", "application/pdf", b"%PDF")])
        text = raw.decode(errors="replace")
        self.assertIn("In-Reply-To: <abc@x>", text)
        self.assertIn("References: <first@x> <abc@x>", text)
        self.assertIn('filename="a.pdf"', text)

    def test_token_round_trip(self):
        with patch.dict(os.environ, {"MAIL_TOKEN_KEY": Fernet.generate_key().decode()}):
            blob = mail_gmail.encrypt_token("refresh-123")
            self.assertNotIn("refresh-123", blob)
            self.assertEqual(mail_gmail.decrypt_token(blob), "refresh-123")
        with patch.dict(os.environ, {"MAIL_TOKEN_KEY": "not-a-key"}):
            self.assertIn("MAIL_TOKEN_KEY (not a valid key)", mail_gmail.setup_missing())
            with self.assertRaises(mail_gmail.MailError):
                mail_gmail.encrypt_token("x")

    def test_exchange_code_needs_both_scopes_and_refresh_token(self):
        class Resp:
            def __init__(self, code, data):
                self.status_code, self._data = code, data

            def json(self):
                return self._data
        ok = {"refresh_token": "r", "scope": " ".join(mail_gmail.SCOPES)}
        self.assertEqual(mail_gmail.exchange_code("c", "https://x/cb", post=lambda *a, **k: Resp(200, ok)), "r")
        for bad in ({"refresh_token": "r", "scope": mail_gmail.SCOPES[0]}, {"scope": " ".join(mail_gmail.SCOPES)}):
            with self.assertRaises(mail_gmail.MailError):
                mail_gmail.exchange_code("c", "https://x/cb", post=lambda *a, **k: Resp(200, bad))
        with self.assertRaises(mail_gmail.MailError):
            mail_gmail.exchange_code("c", "https://x/cb", post=lambda *a, **k: Resp(400, {}))


class _Setup(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.store_patch = patch.object(store, "SessionLocal", self.Session)
        self.store_patch.start()
        store.clear_cache()
        self.env = patch.dict(os.environ, {"MAIL_INBOX_ENABLED": "true", "MAIL_TOKEN_KEY": Fernet.generate_key().decode(),
                                           "GOOGLE_MAIL_CLIENT_ID": "cid", "GOOGLE_MAIL_CLIENT_SECRET": "secret"})
        self.env.start()
        self.read_only = app.state.read_only
        app.state.read_only = False
        self.fake = mail_fake.FakeGmail(SHOP)
        mail_fake.install(self.fake)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.add(Customer(id=5, first_name="Pat", last_name="Lee", email="Pat@Example.test", phone="6025550101"))
            db.add(Vehicle(id=7, customer_id=5, year=2015, make="Ford", model="Focus"))
            db.add(RepairOrder(id=9, customer_id=5, vehicle_id=7, status="in_progress"))
            mail_sync.connect(db, SHOP, "refresh-token", 1)
            db.commit()

    def tearDown(self):
        mail_fake.uninstall()
        self.env.stop()
        self.store_patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def sync(self):
        with self.Session() as db:
            result = mail_sync.sync(db)
            db.commit()
            return result

    def seed_backfill(self):
        old = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
        self.fake.add(mail_fake.raw_message("b1", "tb1", "U-Haul Roadside <noreply@roadside.uhaul.com>",
                                            "New Roadside Service Request #1", "Battery jump at 35th Ave", when=old), record=False)
        self.fake.add(mail_fake.raw_message("b2", "tb2", "Spectrum <billing@spectrum.com>", "Your statement is ready",
                                            "Amount due $179.00", when=old + timedelta(hours=1), labels=("INBOX",)), record=False)
        self.fake.add(mail_fake.raw_message("b3", "tb3", "Spam <x@example.test>", "Win", "spam", labels=("SPAM",)), record=False)


class SyncTests(_Setup):
    def test_first_sync_copies_mail_quietly(self):
        self.seed_backfill()
        result = self.sync()
        self.assertEqual((result["mode"], result["new"]), ("backfill", 2))
        with self.Session() as db:
            self.assertEqual(db.query(MailMessage).count(), 2)
            self.assertEqual(db.query(DispatchItem).count(), 0)  # no alerts for old mail
            uhaul = db.query(MailThread).filter_by(gmail_thread_id="tb1").one()
            self.assertEqual((uhaul.category, uhaul.unread, uhaul.in_inbox), ("uhaul", True, True))
            self.assertEqual(db.query(MailThread).filter_by(gmail_thread_id="tb2").one().category, "bills")
            self.assertEqual(db.query(MailAccount).one().history_id, str(self.fake.history_id))

    def test_new_urgent_mail_raises_one_alert_per_thread(self):
        self.seed_backfill()
        self.sync()
        self.fake.add(mail_fake.raw_message("n1", "tn1", "U-Haul Roadside <noreply@roadside.uhaul.com>",
                                            "New Roadside Service Request #2", "Lockout at 51st Ave"))
        self.fake.add(mail_fake.raw_message("n2", "tn1", "U-Haul Roadside <noreply@roadside.uhaul.com>",
                                            "RE: New Roadside Service Request #2", "Still open"))
        self.fake.add(mail_fake.raw_message("n3", "tn3", "Shop News <news@example.test>", "Deals", "Sale"))
        result = self.sync()
        self.assertEqual((result["mode"], result["new"]), ("update", 3))
        with self.Session() as db:
            items = db.query(DispatchItem).all()
            self.assertEqual(len(items), 1)
            item = items[0]
            thread = db.query(MailThread).filter_by(gmail_thread_id="tn1").one()
            self.assertEqual(item.quo_conversation_id, f"WEBMAIL{thread.id}")
            self.assertEqual((item.status, item.urgency, item.category), ("needs_owner", "urgent", "email"))
            self.assertEqual(item.last_message_id, "mail:n2")
            self.assertEqual(item.customer_phone, "")
            self.assertTrue(thread.urgent)
            self.assertEqual(thread.message_count, 2)

    def test_customer_email_links_job_and_files_communication(self):
        self.sync()
        self.fake.add(mail_fake.raw_message("c1", "tc1", "Pat Lee <pat@example.test>", "Is my Focus ready?", "Checking in"))
        self.sync()
        with self.Session() as db:
            thread = db.query(MailThread).filter_by(gmail_thread_id="tc1").one()
            self.assertEqual((thread.customer_id, thread.repair_order_id, thread.category), (5, 9, "leads"))
            comm = db.query(Communication).filter_by(channel="email").one()
            self.assertEqual((comm.customer_id, comm.direction, comm.external_id), (5, "inbound", "gmail:c1"))
            item = db.query(DispatchItem).one()
            self.assertEqual((item.customer_id, item.customer_phone, item.urgency), (5, "6025550101", "normal"))

    def test_archived_in_gmail_closes_alert(self):
        self.sync()
        self.fake.add(mail_fake.raw_message("u1", "tu1", "billing@spectrum.com", "Service will be disconnected", "Pay now"))
        self.sync()
        self.fake.relabel("u1", ["UNREAD"])  # archived in Gmail
        self.sync()
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).one().status, "closed")
            self.assertFalse(db.query(MailThread).filter_by(gmail_thread_id="tu1").one().in_inbox)

    def test_old_history_point_resyncs_without_alerts(self):
        self.sync()
        self.fake.add(mail_fake.raw_message("r1", "tr1", "noreply@roadside.uhaul.com", "Roadside offer", "Tow"), record=False)
        self.fake.history_gone = True
        result = self.sync()
        self.assertEqual((result["mode"], result["new"]), ("resync", 1))
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).count(), 0)

    def test_gmail_failure_is_saved_and_not_raised(self):
        self.fake.fail = mail_gmail.MailError("The mailbox sign-in expired or was removed. Connect the mailbox again.")
        result = self.sync()
        self.assertFalse(result["ok"])
        with self.Session() as db:
            acct = db.query(MailAccount).one()
            self.assertEqual(acct.status, "error")
            self.assertIn("sign-in expired", acct.last_error)
        self.assertTrue(self.sync()["ok"])  # recovers on the next check

    def test_moved_thread_keeps_its_tab(self):
        self.sync()
        self.fake.add(mail_fake.raw_message("k1", "tk1", "friend@example.test", "hello", "hi"))
        self.sync()
        with self.Session() as db:
            thread = db.query(MailThread).filter_by(gmail_thread_id="tk1").one()
            thread.category, thread.category_locked = "vendors", True
            db.commit()
        self.fake.add(mail_fake.raw_message("k2", "tk1", "orders@example.test", "Form submission", "x"))
        self.sync()
        with self.Session() as db:
            self.assertEqual(db.query(MailThread).filter_by(gmail_thread_id="tk1").one().category, "vendors")

    def test_token_is_stored_encrypted(self):
        with self.Session() as db:
            acct = db.query(MailAccount).one()
            self.assertNotIn("refresh-token", acct.token_encrypted)
            self.assertEqual(mail_gmail.decrypt_token(acct.token_encrypted), "refresh-token")


class PageTests(_Setup):
    def setUp(self):
        super().setUp()
        self.seed_backfill()
        self.sync()  # first connect: old mail, no alerts
        self.fake.add(mail_fake.raw_message(
            "p1", "tp1", "Squarespace <form-submission@squarespace.info>", "Form Submission - Get a quote",
            "Name: Jo Diaz\nCar: 2012 Civic\nNeeds brakes\n\nOn Tue, Nov 25, 2026 at 9:00 AM Shop wrote:\n> earlier",
            html='<p>Name: Jo Diaz</p><img src="https://tracker.example.test/p.gif"><script>alert(1)</script>',
            reply_to="Jo Diaz <jo@example.test>", attachments=[("car.jpg", "image/jpeg", b"\xff\xd8jpeg")]))
        self.sync()
        with self.Session() as db:
            self.thread_id = db.query(MailThread).filter_by(gmail_thread_id="tp1").one().id
        self.client = self.sign_in()

    def test_office_and_signed_out_cannot_open_email(self):
        self.assertFalse(allowed("office", "GET", "/mail"))
        self.assertFalse(allowed("admin", "GET", "/mail/t/1"))
        self.assertTrue(allowed("owner", "GET", "/mail"))
        office = self.sign_in("office")
        self.assertIn(office.get("/mail").status_code, (303, 403))
        anon = TestClient(app, follow_redirects=False)
        self.assertIn(anon.get("/mail").status_code, (303, 401))

    def test_inbox_lists_and_tabs_filter(self):
        page = self.client.get("/mail")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Form Submission - Get a quote", page.text)
        self.assertIn("New Roadside Service Request #1", page.text)
        leads = self.client.get("/mail?tab=leads")
        self.assertIn("Get a quote", leads.text)
        self.assertNotIn("Roadside Service Request #1", leads.text)
        found = self.client.get("/mail?q=civic")
        self.assertIn("Get a quote", found.text)
        self.assertNotIn("Your statement is ready", found.text)
        self.assertIn("Nothing matches", self.client.get("/mail?q=zzzz").text)

    def test_thread_shows_text_marks_read_and_hides_quote(self):
        page = self.client.get(f"/mail/t/{self.thread_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Needs brakes", page.text)
        self.assertIn("Show earlier text", page.text)
        self.assertIn('value="jo@example.test"', page.text)  # reply goes to the customer, not the form robot
        self.assertIn(("tp1", (), ("UNREAD",)), self.fake.modified)
        self.assertNotIn("<script>alert(1)</script>", page.text)

    def test_original_html_is_sandboxed_with_images_held(self):
        with self.Session() as db:
            mid = db.query(MailMessage).filter_by(gmail_message_id="p1").one().id
        page = self.client.get(f"/mail/m/{mid}/html")
        self.assertEqual(page.status_code, 200)
        csp = page.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("img-src data:;", csp)
        self.assertIn("sandbox", csp)
        self.assertNotIn("script-src", csp)
        self.assertEqual(page.headers["x-frame-options"], "SAMEORIGIN")
        self.assertIn("img-src data: https:", self.client.get(f"/mail/m/{mid}/html?images=1").headers["content-security-policy"])

    def test_attachment_download(self):
        with self.Session() as db:
            att = db.query(MailAttachment).one()
        resp = self.client.get(f"/mail/a/{att.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"\xff\xd8jpeg")
        self.assertIn("inline", resp.headers["content-disposition"])

    def _key(self):
        import re
        page = self.client.get(f"/mail/t/{self.thread_id}")
        return re.search(r'name="send_key" value="([^"]+)"', page.text).group(1)

    def test_reply_sends_once_threads_and_closes_alert(self):
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).filter_by(status="needs_owner").count(), 1)
        key = self._key()
        form = {"to": "jo@example.test", "cc": "", "body": "Hi Jo, I can do your brakes Tuesday.", "sign": "1",
                "send_key": key}
        resp = self.client.post(f"/mail/t/{self.thread_id}/reply", data=form,
                                files=[("files", ("quote.pdf", b"%PDF-1", "application/pdf"))])
        self.assertEqual(resp.status_code, 303, resp.text[:300])
        again = self.client.post(f"/mail/t/{self.thread_id}/reply", data=form)  # double tap
        self.assertEqual(again.status_code, 303)
        self.assertEqual(len(self.fake.sent), 1)
        sent = self.fake.sent[0]
        self.assertEqual(sent["to"], "jo@example.test")
        self.assertEqual(sent["subject"], "Re: Form Submission - Get a quote")
        self.assertEqual(sent["in_reply_to"], "<p1@mail.example.test>")
        self.assertEqual(sent["thread"], "tp1")
        self.assertIn("Imperial Auto Care\n(480) 914-4144", sent["body"])
        self.assertEqual(sent["files"], ["quote.pdf"])
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).filter_by(status="needs_owner").count(), 0)
            thread = db.get(MailThread, self.thread_id)
            self.assertEqual(thread.message_count, 2)
            self.assertEqual(thread.last_from, "Me")

    def test_reply_errors_keep_the_draft(self):
        key = self._key()
        bad = self.client.post(f"/mail/t/{self.thread_id}/reply", data={"to": "not an address", "body": "keep me", "send_key": key})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("keep me", bad.text)
        self.fake.fail = mail_gmail.MailError("Gmail couldn't be reached.")
        failed = self.client.post(f"/mail/t/{self.thread_id}/reply", data={"to": "jo@example.test", "body": "keep me too", "send_key": key})
        self.assertEqual(failed.status_code, 502)
        self.assertIn("Not sent.", failed.text)
        self.assertIn("keep me too", failed.text)
        self.assertEqual(self.fake.sent, [])
        nokey = self.client.post(f"/mail/t/{self.thread_id}/reply", data={"to": "jo@example.test", "body": "no key"})
        self.assertEqual(nokey.status_code, 400)  # never claims "Sent." when it didn't send
        self.assertIn("no key", nokey.text)
        ok = self.client.post(f"/mail/t/{self.thread_id}/reply", data={"to": "jo@example.test", "body": "now", "send_key": key})
        self.assertEqual(ok.status_code, 303)  # a failed send frees the form to try again
        self.assertEqual(len(self.fake.sent), 1)

    def test_stop_all_blocks_email_sends(self):
        with self.Session() as db:
            db.add(DispatchAudit(item_id=None, actor="owner", action="sending_paused"))
            db.commit()
        resp = self.client.post(f"/mail/t/{self.thread_id}/reply", data={"to": "jo@example.test", "body": "x", "send_key": self._key()})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.fake.sent, [])

    def test_archive_star_unread_move(self):
        resp = self.client.post(f"/mail/t/{self.thread_id}/archive", data={"next": "/mail"})
        self.assertEqual(resp.headers["location"], "/mail?note=archived")
        with self.Session() as db:
            thread = db.get(MailThread, self.thread_id)
            self.assertFalse(thread.in_inbox)
            self.assertEqual(db.query(DispatchItem).one().status, "closed")
        self.assertNotIn("Get a quote", self.client.get("/mail").text)
        self.client.post(f"/mail/t/{self.thread_id}/inbox")
        self.client.post(f"/mail/t/{self.thread_id}/star", data={"on": "1"})
        self.client.post(f"/mail/t/{self.thread_id}/unread")
        self.client.post(f"/mail/t/{self.thread_id}/category", data={"category": "vendors"})
        with self.Session() as db:
            thread = db.get(MailThread, self.thread_id)
            self.assertEqual((thread.in_inbox, thread.starred, thread.unread, thread.category, thread.category_locked),
                             (True, True, True, "vendors", True))
        self.assertEqual(self.client.post(f"/mail/t/{self.thread_id}/category", data={"category": "bogus"}).status_code, 400)
        self.assertIn("Get a quote", self.client.get("/mail?box=starred").text)
        self.assertEqual(self.client.post(f"/mail/t/{self.thread_id}/archive", data={"next": "https://evil.example"}).headers["location"],
                         "/mail?note=archived")

    def test_gmail_refusing_a_label_change_shows_error(self):
        self.fake.fail = mail_gmail.MailError("Gmail said no (500).")
        resp = self.client.post(f"/mail/t/{self.thread_id}/archive", data={"next": "/mail"})
        self.assertEqual((resp.status_code, resp.headers["location"]), (303, f"/mail/t/{self.thread_id}?err=gmail"))
        self.assertIn("nothing moved", self.client.get(resp.headers["location"]).text)
        with self.Session() as db:
            self.assertTrue(db.get(MailThread, self.thread_id).in_inbox)

    def test_link_customer_and_save_email(self):
        with self.Session() as db:
            db.add(Customer(id=6, first_name="Jo", last_name="Diaz", phone="6025550199"))
            db.commit()
        page = self.client.get(f"/mail/t/{self.thread_id}?find=diaz")
        self.assertIn("Jo Diaz", page.text)
        resp = self.client.post(f"/mail/t/{self.thread_id}/link", data={"customer_id": "6", "save_email": "1"})
        self.assertEqual(resp.status_code, 303)
        with self.Session() as db:
            self.assertEqual(db.get(MailThread, self.thread_id).customer_id, 6)
            self.assertEqual(db.get(Customer, 6).email, "jo@example.test")  # the person, not the form robot
        self.assertEqual(self.client.post(f"/mail/t/{self.thread_id}/link", data={"customer_id": "6", "repair_order_id": "9"}).status_code, 400)
        self.client.post(f"/mail/t/{self.thread_id}/link", data={"customer_id": "none"})
        with self.Session() as db:
            self.assertIsNone(db.get(MailThread, self.thread_id).customer_id)

    def test_compose_new_email(self):
        import re
        page = self.client.get("/mail/compose?customer_id=5")
        self.assertIn('value="Pat@Example.test"', page.text)
        key = re.search(r'name="send_key" value="([^"]+)"', page.text).group(1)
        missing = self.client.post("/mail/compose", data={"to": "pat@example.test", "subject": "", "body": "x", "send_key": key})
        self.assertEqual(missing.status_code, 400)
        resp = self.client.post("/mail/compose", data={"to": "pat@example.test", "subject": "Your Focus", "body": "Ready today.",
                                                       "send_key": key, "customer_id": "5"})
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(self.fake.sent[0]["subject"], "Your Focus")
        self.assertNotIn("(480) 914-4144", self.fake.sent[0]["body"])  # signature box left unticked
        with self.Session() as db:
            thread = db.query(MailThread).filter_by(gmail_thread_id="t-sent1").one()
            self.assertEqual(thread.customer_id, 5)

    def test_queue_row_links_to_the_email(self):
        page = self.client.get("/dashboard/inbox")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"/mail/t/{self.thread_id}", page.text)
        self.assertNotIn("contact this customer by phone", page.text.split(f"/mail/t/{self.thread_id}")[0][-400:])

    def test_settings_and_check_now(self):
        page = self.client.get("/mail/settings")
        self.assertEqual(page.status_code, 200)
        self.assertIn(SHOP, page.text)
        self.assertNotIn("refresh-token", page.text)
        self.fake.add(mail_fake.raw_message("s1", "ts1", "friend@example.test", "new one", "hi"))
        resp = self.client.post("/mail/sync", data={"next": "/mail"})
        self.assertEqual(resp.headers["location"], "/mail?note=synced")
        self.assertIn("new one", self.client.get("/mail").text)

    def test_disconnect_forgets_token(self):
        self.client.post("/mail/disconnect")
        with self.Session() as db:
            acct = db.query(MailAccount).one()
            self.assertEqual((acct.status, acct.token_encrypted), ("disconnected", None))
        self.assertEqual(self.client.get("/mail").headers["location"], "/mail/settings")


class ConnectTests(_Setup):
    def setUp(self):
        super().setUp()
        with self.Session() as db:
            db.query(MailAccount).delete()
            db.commit()
        self.client = self.sign_in()

    def test_connect_round_trip(self):
        page = self.client.get("/mail/settings")
        self.assertIn("Connect Gmail", page.text)
        go = self.client.get("/mail/connect")
        self.assertEqual(go.status_code, 303)
        self.assertTrue(go.headers["location"].startswith(mail_gmail.AUTH_URL))
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(go.headers["location"]).query)
        self.assertEqual(params["access_type"], ["offline"])
        self.assertIn("gmail.send", params["scope"][0])
        state = params["state"][0]
        bad = self.client.get("/mail/oauth/callback?state=wrong&code=x")
        self.assertEqual(bad.headers["location"], "/mail/settings?error=state")
        self.client.get("/mail/connect")
        state = parse_qs(urlparse(self.client.get("/mail/connect").headers["location"]).query)["state"][0]
        with patch.object(mail_gmail, "exchange_code", return_value="new-refresh"):
            done = self.client.get(f"/mail/oauth/callback?state={state}&code=abc")
        self.assertEqual(done.headers["location"], "/mail/settings?note=connected")
        with self.Session() as db:
            acct = db.query(MailAccount).one()
            self.assertEqual(acct.address, SHOP)
            self.assertEqual(mail_gmail.decrypt_token(acct.token_encrypted), "new-refresh")

    def test_missing_setup_is_listed(self):
        with patch.dict(os.environ, {"GOOGLE_MAIL_CLIENT_ID": ""}):
            page = self.client.get("/mail/settings")
            self.assertIn("GOOGLE_MAIL_CLIENT_ID", page.text)
            self.assertEqual(self.client.get("/mail/connect").headers["location"], "/mail/settings")


class OffTests(_Setup):
    def test_off_until_enabled_and_audit_skips_tables(self):
        client = self.sign_in()
        with patch.dict(os.environ, {"MAIL_INBOX_ENABLED": ""}):
            page = client.get("/mail")
            self.assertEqual(page.status_code, 200)
            self.assertIn("isn't turned on yet", page.text)
            self.assertNotIn('href="/mail"', client.get("/dashboard").text)
            from app.recovery import database_audit
            with patch("app.recovery.engine", self.engine):
                audit = database_audit(include_counts=False)
            self.assertFalse(set(audit["missing_tables"]) & {"mail_accounts", "mail_threads"})
        self.assertIn('href="/mail"', client.get("/dashboard").text)


if __name__ == "__main__":
    unittest.main()
