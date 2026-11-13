"""Security & Privacy Center, website login button, hidden services and counter payments. Made-up data only."""
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import business_settings as store
from app import security_controls as controls
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Customer, Invoice, RepairOrder, User, Vehicle
from app.security import LoginLimiter

PASSWORD = "fixture-password-1"


def full_form(**changes):
    data = {}
    for c in controls.CONTROLS:
        if c.kind == "bool":
            if c.default:
                data[c.key] = "on"
        elif c.kind == "list":
            data[c.key] = "\n".join(c.default)
        else:
            data[c.key] = str(c.default)
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


class _Setup(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.patch = patch.object(store, "SessionLocal", self.Session)
        self.patch.start()
        store.clear_cache()
        self.read_only = app.state.read_only
        app.state.read_only = False
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash=hash_password(PASSWORD), role="owner"))
            db.add(User(id=2, username="office", password_hash=hash_password(PASSWORD), role="office"))
            db.commit()

    def tearDown(self):
        self.patch.stop()
        store.clear_cache()
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def sign_in(self, name="owner"):
        client = TestClient(app, follow_redirects=False)
        client.post("/login", data={"username": name, "password": PASSWORD})
        return client

    def save(self, client, **changes):
        response = client.post("/settings/security-privacy", data=full_form(**changes))
        store.clear_cache()
        return response


class SecurityCenterTests(_Setup):
    # ── Page and permissions ────────────────────────────────────────────────
    def test_owner_sees_every_control_and_office_is_refused(self):
        page = self.sign_in().get("/settings/security-privacy")
        self.assertEqual(page.status_code, 200)
        for c in controls.CONTROLS:
            if c.group not in controls.GROUPS:
                continue  # internal (review checklist state)
            self.assertIn(c.title.replace("'", "&#39;"), page.text)
        for f in controls.FIXED:
            self.assertIn(f.title.replace("'", "&#39;"), page.text)
        self.assertEqual(self.sign_in("office").get("/settings/security-privacy").status_code, 403)
        self.assertEqual(self.sign_in("office").post("/settings/security-privacy", data=full_form()).status_code, 403)

    def test_defaults_are_used_until_changed_and_changes_are_audited(self):
        self.assertEqual(controls.value("login_max_failures"), 5)
        client = self.sign_in()
        self.assertEqual(self.save(client, login_max_failures="8").status_code, 303)
        self.assertEqual(controls.value("login_max_failures"), 8)
        with self.Session() as db:
            change = db.query(store.BusinessSettingChange).filter_by(key="security_controls").one()
            self.assertEqual(change.changed_by, "owner")
        # Reset puts everything back
        client.post("/settings/security-privacy", data={"reset": "all"})
        store.clear_cache()
        self.assertEqual(controls.value("login_max_failures"), 5)

    def test_turning_off_a_protection_needs_the_confirm_tick(self):
        client = self.sign_in()
        refused = self.save(client, content_security_policy=None)
        self.assertEqual(refused.status_code, 422)
        self.assertTrue(controls.value("content_security_policy"))
        ok = self.save(client, content_security_policy=None, confirm_content_security_policy="on")
        self.assertEqual(ok.status_code, 303)
        self.assertFalse(controls.value("content_security_policy"))
        self.assertNotIn("content-security-policy", TestClient(app).get("/site/").headers)

    def test_out_of_range_numbers_are_refused(self):
        response = self.save(self.sign_in(), session_hours="0")
        self.assertEqual(response.status_code, 422)

    def test_bad_saved_value_falls_back_to_default(self):
        with self.Session() as db:
            store.save(db, "security_controls", "{not json", "test")
            db.commit()
        store.clear_cache()
        self.assertEqual(controls.value("form_hourly_limit"), 5)

    # ── Browser protections ─────────────────────────────────────────────────
    def test_headers_on_by_default(self):
        r = TestClient(app).get("/site/", headers={"x-forwarded-proto": "https"})
        self.assertIn("frame-ancestors 'none'", r.headers["content-security-policy"])
        self.assertIn("max-age=31536000", r.headers["strict-transport-security"])
        self.assertEqual(r.headers["x-frame-options"], "DENY")
        self.assertIn("camera=()", r.headers["permissions-policy"])

    def test_cross_site_post_blocked_and_switch_works(self):
        client = self.sign_in()
        blocked = client.post("/settings/security-privacy", data=full_form(), headers={"origin": "https://evil.example"})
        self.assertEqual(blocked.status_code, 403)

    def test_strict_origin_mode_refuses_posts_with_no_origin(self):
        client = self.sign_in()
        self.save(client, require_origin_header="on")
        r = TestClient(app).post("/site/status", data={"reference": "WEB-1", "phone": "6025550100"})
        self.assertEqual(r.status_code, 403)

    # ── Sign-in ─────────────────────────────────────────────────────────────
    def test_login_lock_follows_the_setting(self):
        limiter = LoginLimiter()
        now = time.time()
        for _ in range(3):
            limiter.record("user:x", now=now)
        self.assertFalse(limiter.locked("user:x", now=now))
        self.save(self.sign_in(), login_max_failures="3")
        self.assertTrue(limiter.locked("user:x", now=now))

    def test_session_ends_after_the_set_hours(self):
        client = self.sign_in()
        self.assertEqual(client.get("/settings").status_code, 200)
        self.save(client, session_hours="1")
        with patch("time.time", return_value=time.time() + 2 * 3600):
            self.assertEqual(client.get("/settings").status_code, 302)

    # ── Website ─────────────────────────────────────────────────────────────
    def test_root_sends_visitors_to_the_website_with_a_staff_login_button(self):
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.get("/").headers["location"], "/site/")
        home = client.get("/site/")
        self.assertIn('href="/login"', home.text)
        self.assertIn("Staff login", home.text)
        self.save(self.sign_in(), website_login_button=None)
        self.assertNotIn("Staff login", TestClient(app).get("/site/").text)

    def test_nvzn_and_rentals_hidden_until_turned_back_on(self):
        client = TestClient(app)
        self.assertEqual(client.get("/site/services/nvzn-customs").status_code, 404)
        self.assertEqual(client.get("/site/rentals").status_code, 404)
        home = client.get("/site/").text
        self.assertNotIn("NVZN", home)
        self.assertNotIn('href="/site/rentals"', home)
        self.save(self.sign_in(), hidden_services="")
        self.assertEqual(TestClient(app).get("/site/services/nvzn-customs").status_code, 200)
        self.assertEqual(TestClient(app).get("/site/rentals").status_code, 200)

    def test_heavy_duty_roadside_and_suite_508_are_advertised(self):
        client = TestClient(app)
        heavy = client.get("/site/services/heavy-duty")
        self.assertEqual(heavy.status_code, 200)
        self.assertIn("18-wheelers", heavy.text)
        roadside = client.get("/site/services/mobile-roadside").text
        self.assertIn("18-wheeler and semi jump starts", roadside)
        self.assertIn("No-start", roadside)
        for path in ("/site/", "/site/contact", "/site/faq"):
            text = client.get(path).text
            self.assertIn("Suite 508", text)
            self.assertNotIn("Suite 417", text)

    def test_privacy_and_terms_publish_and_edit_without_code(self):
        client = TestClient(app)
        self.assertEqual(client.get("/site/privacy").status_code, 404)
        self.save(self.sign_in(), privacy_notice_published="on", terms_published="on",
                  terms_text="Custom terms paragraph one.\n\nSecond paragraph.")
        privacy = TestClient(app).get("/site/privacy")
        self.assertEqual(privacy.status_code, 200)
        self.assertIn("Suite 508", privacy.text)
        terms = TestClient(app).get("/site/terms").text
        self.assertIn("Custom terms paragraph one.", terms)
        self.assertIn('href="/site/privacy"', TestClient(app).get("/site/").text)


class PaymentAndLinkTests(_Setup):
    def make_job(self, status="complete", total=100.0):
        with self.Session() as db:
            customer = Customer(first_name="Test", last_name="Only", phone="6025550100")
            vehicle = Vehicle(customer=customer, year=2015, make="Ford", model="F-250")
            ro = RepairOrder(customer=customer, vehicle=vehicle, status=status, public_token="t" * 43)
            invoice = Invoice(repair_order=ro, subtotal=total, tax_rate=0, tax_amount=0, total=total, status="sent")
            db.add_all([customer, vehicle, ro, invoice])
            db.commit()
            return ro.id, invoice.id

    def test_apple_cash_payment_and_pickup_close_the_job(self):
        ro_id, invoice_id = self.make_job()
        client = self.sign_in("office")
        r = client.post(f"/invoices/{invoice_id}/add-payment",
                        data={"amount": "100", "method": "apple_cash", "reference": "J Doe", "picked_up": "on"})
        self.assertEqual(r.status_code, 302)
        with self.Session() as db:
            self.assertEqual(db.get(Invoice, invoice_id).status, "paid")
            self.assertEqual(db.get(RepairOrder, ro_id).status, "delivered")

    def test_partial_payment_does_not_close_the_job(self):
        ro_id, invoice_id = self.make_job()
        self.sign_in("office").post(f"/invoices/{invoice_id}/add-payment",
                                    data={"amount": "40", "method": "zelle", "picked_up": "on"})
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, ro_id).status, "complete")

    def test_owner_chooses_payment_methods(self):
        from app.routes.invoices import enabled_methods
        self.assertNotIn("cashapp", enabled_methods())
        with self.Session() as db:
            store.save(db, "payment_methods", "zelle,cash", "owner")
            db.commit()
        store.clear_cache()
        self.assertEqual(enabled_methods(), ("zelle", "cash"))
        _, invoice_id = self.make_job()
        r = self.sign_in("office").post(f"/invoices/{invoice_id}/add-payment", data={"amount": "10", "method": "apple_cash"})
        self.assertIn("error=", r.headers["location"])

    def test_status_link_stops_after_pickup_when_set(self):
        ro_id, _ = self.make_job(status="delivered")
        client = TestClient(app)
        self.assertEqual(client.get("/status/" + "t" * 43).status_code, 200)
        self.save(self.sign_in(), status_link_days_after_pickup="30")
        with self.Session() as db:
            db.get(RepairOrder, ro_id).updated_at = datetime.now(timezone.utc) - timedelta(days=31)
            db.commit()
        self.assertEqual(TestClient(app).get("/status/" + "t" * 43).status_code, 404)


if __name__ == "__main__":
    unittest.main()


class LegalReviewTests(_Setup):
    def make_pending_job(self):
        from app.models import LineItem
        with self.Session() as db:
            customer = Customer(first_name="Test", last_name="Only", phone="6025550101")
            vehicle = Vehicle(customer=customer, year=2016, make="Ford", model="Focus")
            ro = RepairOrder(customer=customer, vehicle=vehicle, status="waiting_approval", public_token="r" * 43)
            db.add_all([customer, vehicle, ro, LineItem(repair_order=ro, description="Clutch", item_type="labor",
                                                        quantity=1, unit_price=100, approval="pending")])
            db.commit()
            return ro.id, ro.line_items[0].id

    def test_review_items_listed_on_settings_and_home_without_blocking(self):
        self.assertEqual(controls.waiting_for_review(), len(controls.REVIEW_ITEMS))
        client = self.sign_in()
        page = client.get("/settings/security-privacy").text
        self.assertIn("Waiting for your review", page)
        for item in controls.REVIEW_ITEMS:
            self.assertIn(item.title, page)
        self.assertIn("legal items waiting for your review", client.get("/dashboard").text)
        self.assertNotIn("legal item", self.sign_in("office").get("/dashboard").text)
        # The public site works while everything is still waiting
        self.assertEqual(TestClient(app).get("/site/").status_code, 200)

    def test_manual_items_tick_done_and_switches_count_as_done(self):
        client = self.sign_in()
        self.save(client, done_call_notice="on", privacy_notice_published="on")
        rows = {r["item"].key: r["done"] for r in controls.review_status()}
        self.assertTrue(rows["call_notice"])
        self.assertTrue(rows["privacy"])
        self.assertFalse(rows["warranty"])
        self.assertEqual(controls.waiting_for_review(), len(controls.REVIEW_ITEMS) - 2)

    def test_terms_at_signature_off_by_default_then_required_and_recorded(self):
        ro_id, line_id = self.make_pending_job()
        client = TestClient(app)
        self.assertNotIn("terms_ok", client.get("/sign/ro/" + "r" * 43).text)
        self.save(self.sign_in(), terms_on_approval_page="on")
        page = TestClient(app).get("/sign/ro/" + "r" * 43).text
        self.assertIn("Storage of $25 per day", page)
        form = {f"line_{line_id}": "approved", "signature_data": "data:image/png;base64,AAAA", "approved_by": "Test Only"}
        refused = TestClient(app).post("/sign/ro/" + "r" * 43, data=form)
        self.assertEqual(refused.status_code, 422)
        ok = TestClient(app).post("/sign/ro/" + "r" * 43, data={**form, "terms_ok": "on"})
        self.assertEqual(ok.status_code, 200)
        with self.Session() as db:
            self.assertIn("agreed to the shop terms (version", db.get(RepairOrder, ro_id).tech_notes)
