"""Reply desk: context-aware drafts, clean-up of rough replies, and the live queue rows.

Synthetic data only. No network: the AI clean-up call is patched. The central promise is
checked directly: drafting, cleaning up and refreshing never queue a text (DispatchOutbound)
and never change the queue item. Only the owner's Send does that.
"""
import io
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import fixture_environment  # noqa

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import reply_context, reply_drafts
from app.appointment_model import Appointment
from app.database import Base, get_db
from app.dispatch_model import DispatchItem, DispatchOutbound
from app.main import app
from app.models import Communication, Customer, Invoice, LineItem, PartsOrder, Payment, RepairOrder, User, Vehicle

FIXTURES = Path(__file__).parent / "fixtures" / "reply_threads"
NOW = datetime(2026, 11, 25, 18, 0, tzinfo=timezone.utc)


def ctx(text, job=None, first="Testy", vehicle="2015 Ford F-250", outbound=("Hi Testy, got it. - Jae",)):
    conversation = [{"direction": "outbound", "body": b} for b in outbound]
    conversation.append({"direction": "inbound", "body": text})
    return {"customer": {"first_name": first}, "vehicle": {"label": vehicle} if vehicle else None,
            "conversation": conversation, "job": job,
            "style": reply_context.style_from(list(outbound)), "now": NOW.isoformat()}


def job(status, **extra):
    base = {"id": 7, "status": status, "total": None, "deposits_paid": 0, "invoice": None, "parts": [],
            "appointment": None, "promised_at": None, "status_link": None, "vehicle": "2015 Ford F-250",
            "approvals": {"pending": 0, "approved": 0, "declined": 0, "not_sent": 0}}
    base.update(extra)
    return base


class SignOffTests(unittest.TestCase):
    def test_one_jae_sign_off_whatever_was_there(self):
        for text in ("Your truck is ready.", "Your truck is ready. - Jae", "Your truck is ready.\n-Jae",
                     "Your truck is ready. - Imperial Auto Care", "Your truck is ready. Thanks, Jae",
                     "Your truck is ready. – Jaelan"):
            signed = reply_drafts.sign(text)
            self.assertEqual(signed, "Your truck is ready.\n- Jae", text)
            self.assertEqual(reply_drafts.sign(signed), signed)

    def test_snap_line_still_goes_before_the_sign_off(self):
        from app import snap_offer
        text = snap_offer.add(reply_drafts.sign("Total is $500."), True)
        self.assertTrue(text.endswith("- Jae"))
        self.assertIn("Snap Finance", text)


class DraftTests(unittest.TestCase):
    def assertSigned(self, draft):
        self.assertTrue(draft.text.endswith("\n- Jae"), draft.text)
        self.assertEqual(draft.text.count("Jae"), 1, draft.text)
        self.assertNotIn("Snap", draft.text)  # only when he ticks it

    def test_waiting_on_approval_gives_total_and_link(self):
        d = reply_drafts.draft(ctx("how much is it gonna be?", job(
            "waiting_approval", total=1234.5, status_link="https://imperialautophx.com/status/abc",
            approvals={"pending": 3, "approved": 0, "declined": 0, "not_sent": 0})))
        self.assertIn("$1,234.50", d.text)
        self.assertIn("https://imperialautophx.com/status/abc", d.text)
        self.assertTrue(d.text.startswith("Hi Testy, "))
        self.assertEqual(d.needs_info, [])
        self.assertSigned(d)

    def test_approval_by_text_is_acknowledged_and_flagged_to_record(self):
        d = reply_drafts.draft(ctx("Go ahead and do it", job(
            "waiting_approval", total=500, approvals={"pending": 2, "approved": 0, "declined": 0, "not_sent": 0})))
        self.assertEqual(d.intent, "approve")
        self.assertTrue(any("record the approval on job #7" in n for n in d.needs_info))

    def test_parts_without_eta_is_honest_and_flagged(self):
        d = reply_drafts.draft(ctx("when will it be done?", job(
            "waiting_parts", parts=[{"description": "fuel injector", "status": "ordered"}])))
        self.assertIn("waiting on the fuel injector", d.text)
        self.assertNotRegex(d.text, r"\b(today|tomorrow|Monday|\d{1,2}(:\d\d)? ?(AM|PM))\b")
        self.assertTrue(any(n.startswith("parts ETA") for n in d.needs_info))
        self.assertSigned(d)

    def test_unknown_finish_time_says_so(self):
        d = reply_drafts.draft(ctx("when will it be done", job("in_progress")))
        self.assertIn("Let me check on timing", d.text)
        self.assertIn("finish time: no promised date on job #7", d.needs_info)

    def test_promised_date_is_used_in_shop_time(self):
        d = reply_drafts.draft(ctx("when will it be done", job("in_progress", promised_at="2026-11-27T01:00:00+00:00")))
        self.assertIn("Thursday, Nov 26", d.text)  # 6 PM Phoenix on the 26th, though it's already the 27th in UTC
        self.assertEqual(d.needs_info, [])

    def test_ready_for_pickup_with_balance(self):
        d = reply_drafts.draft(ctx("can I pick it up today?", job(
            "complete", total=840, invoice={"status": "sent", "total": 840, "paid": 420, "balance": 420})))
        self.assertIn("ready for pickup", d.text)
        self.assertIn("$420.00", d.text)
        self.assertIn("11 AM-7 PM", d.text)

    def test_no_job_price_question_quotes_nothing(self):
        d = reply_drafts.draft(ctx("how much for brakes?", None))
        self.assertNotIn("$", d.text)
        self.assertIn("price asked: nothing quoted yet", d.needs_info)

    def test_no_job_status_question_is_flagged(self):
        d = reply_drafts.draft(ctx("is my truck ready?", None))
        self.assertIn("let me check on that", d.text)
        self.assertIn("no open job on file for this customer", d.needs_info)

    def test_unanswerable_question_is_flagged_with_the_question(self):
        d = reply_drafts.draft(ctx("do you guys sell tires?", job("in_progress")))
        self.assertIn("get back to you shortly", d.text)
        self.assertTrue(d.needs_info and "sell tires" in d.needs_info[0])

    def test_safety_first(self):
        d = reply_drafts.draft(ctx("it's smoking and smells like fuel", job("in_progress")))
        self.assertEqual(d.intent, "safety")
        self.assertIn("don't drive it", d.text)

    def test_style_follows_his_past_texts(self):
        outbound = ("Hey Mike, it's in the bay now. - Jae", "Hey Mike, parts are here. - Jae",
                    "Hey, all good. - Jae")
        d = reply_drafts.draft(ctx("thanks", None, first="Mike", outbound=outbound))
        self.assertTrue(d.text.startswith("Hey Mike, "), d.text)
        self.assertNotIn("!", d.text)

    def test_existing_ai_draft_is_shown_signed_with_rule_flags(self):
        class Item:
            draft_reply = "We're still waiting on the injector, I'll update you."
        d = reply_drafts.card_draft(Item(), ctx("when will it be done", job("in_progress")))
        self.assertEqual(d.source, "ai")
        self.assertTrue(d.text.endswith("\n- Jae"))
        self.assertIn("finish time: no promised date on job #7", d.needs_info)


class CleanUpTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"GROQ_API_KEY": "", "REPLY_AI": "on"})
        env.start()
        self.addCleanup(env.stop)

    def test_rough_text_is_tidied_greeted_and_signed_once(self):
        text = reply_drafts.clean_up("hey testy its ready u can grab it after 3 - Jae", ctx("?")).text
        self.assertEqual(text, "Hey Testy, it's ready you can grab it after 3.\n- Jae")

    def test_dictated_relay_is_turned_to_the_customer(self):
        text = reply_drafts.clean_up("let him know his truck is done and he can pick it up tomorrow", ctx("?")).text
        self.assertEqual(text, "Hi Testy, your truck is done and you can pick it up tomorrow.\n- Jae")

    def test_spoken_question_mark(self):
        text = reply_drafts.clean_up("can you come by tomorrow at 2 question mark", ctx("?")).text
        self.assertEqual(text, "Hi Testy, can you come by tomorrow at 2?\n- Jae")

    def test_ai_result_is_used_when_it_adds_no_facts(self):
        os.environ["GROQ_API_KEY"] = "test-only"
        with patch.object(reply_drafts, "_ai_complete", return_value="Hi Testy, your truck is ready after 3. - Jae") as call:
            d = reply_drafts.clean_up("tell him truck ready after 3", ctx("?"))
        call.assert_called_once()
        self.assertEqual((d.source, d.text), ("ai", "Hi Testy, your truck is ready after 3.\n- Jae"))

    def test_ai_result_that_invents_a_price_falls_back_to_rules(self):
        os.environ["GROQ_API_KEY"] = "test-only"
        with patch.object(reply_drafts, "_ai_complete", return_value="Hi Testy, it's ready, the total is $650."):
            d = reply_drafts.clean_up("tell him its ready", ctx("?"))
        self.assertEqual(d.source, "rules")
        self.assertNotIn("$", d.text)

    def test_ai_failure_falls_back_to_rules(self):
        os.environ["GROQ_API_KEY"] = "test-only"
        with patch.object(reply_drafts, "_ai_complete", side_effect=RuntimeError("down")):
            d = reply_drafts.clean_up("its ready", ctx("?"))
        self.assertEqual((d.source, d.text), ("rules", "Hi Testy, it's ready.\n- Jae"))

    def test_no_key_never_calls_ai(self):
        with patch.object(reply_drafts, "_ai_complete") as call:
            reply_drafts.clean_up("its ready", ctx("?"))
        call.assert_not_called()


class PreviewScriptTests(unittest.TestCase):
    def test_saved_threads_print_a_signed_draft(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("draft_preview", Path(__file__).parents[1] / "scripts" / "draft_preview.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for path in sorted(FIXTURES.glob("*.json")):
            out = io.StringIO()
            with patch.dict(os.environ, {}), redirect_stdout(out):
                self.assertEqual(module.main([str(path)]), 0)
            self.assertIn("\n- Jae\n", out.getvalue(), path.name)


class ReplyDeskDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        start = datetime.now(timezone.utc)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash="x", role="owner"))
            db.add(Customer(id=5, first_name="Testy", last_name="McTest", phone="+16235550101"))
            db.add(Vehicle(id=6, customer_id=5, year=2015, make="Ford", model="F-250"))
            db.add(RepairOrder(id=7, customer_id=5, vehicle_id=6, status="waiting_parts", public_token="tok7",
                               concern="TEST ONLY: rough idle"))
            db.add_all([
                LineItem(repair_order_id=7, description="Injector", item_type="part", quantity=1, unit_price=400,
                         approval="approved"),
                LineItem(repair_order_id=7, description="Labor", item_type="labor", quantity=2, unit_price=150,
                         approval="approved"),
                LineItem(repair_order_id=7, description="Glow plugs", item_type="part", quantity=1, unit_price=90,
                         approval="declined"),
            ])
            db.add(PartsOrder(repair_order_id=7, description="Fuel injector", status="ordered"))
            db.add(Appointment(repair_order_id=7, resource_id="bay1", starts_at=start + timedelta(days=1),
                               ends_at=start + timedelta(days=1, hours=1), status="confirmed"))
            db.add_all([
                Communication(customer_id=5, direction="outbound", body="Hey Testy, injector is ordered. - Jae",
                              created_at=datetime(2026, 11, 20, 18, 0)),
                Communication(customer_id=5, direction="inbound", body="ok thanks",
                              created_at=datetime(2026, 11, 20, 18, 5)),
            ])
            db.add(DispatchItem(id=40, quo_conversation_id="CN40", customer_phone="+16235550101", customer_id=5,
                                status="needs_owner", summary="TEST ONLY: when will it be done?",
                                last_message_id="AC40", approval_reasons='["customer_reply"]'))
            db.commit()
        for target in ("app.routes.dispatch.get_current_user", "app.main.get_current_user"):
            login = patch(target, side_effect=lambda request, db: db.get(User, 1))
            login.start()
            self.addCleanup(login.stop)
        env = patch.dict(os.environ, {"GROQ_API_KEY": "", "REPLY_AI": "on"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def nothing_queued_or_changed(self):
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 0)
            item = db.get(DispatchItem, 40)
            self.assertEqual((item.status, item.draft_reply, item.last_message_id), ("needs_owner", None, "AC40"))

    def test_context_has_thread_job_parts_money_and_style(self):
        with self.Session() as db:
            item = db.get(DispatchItem, 40)
            context = reply_context.build(db, 5, item)
        self.assertEqual(context["customer"]["first_name"], "Testy")
        self.assertEqual(context["vehicle"]["label"], "2015 Ford F-250")
        self.assertEqual(reply_context.latest_inbound(context), "TEST ONLY: when will it be done?")
        job = context["job"]
        self.assertEqual((job["id"], job["status"]), (7, "waiting_parts"))
        self.assertEqual(job["approvals"], {"pending": 0, "approved": 2, "declined": 1, "not_sent": 0})
        from app.routes.invoices import SHOP_TAX_RATE
        self.assertEqual(job["total"], round(700 + 400 * SHOP_TAX_RATE, 2))  # tax on the part only; declined line left out
        self.assertEqual(job["parts"][0]["description"], "Fuel injector")
        self.assertIsNone(job["parts"][0]["eta"])
        self.assertEqual(job["appointment"]["status"], "confirmed")
        self.assertTrue(job["status_link"].endswith("/status/tok7"))
        self.assertEqual(context["style"]["greeting"], "Hey")

    def test_invoice_balance_counts_payments(self):
        with self.Session() as db:
            invoice = Invoice(repair_order_id=7, total=735.0, status="sent")
            db.add(invoice)
            db.flush()
            db.add(Payment(invoice_id=invoice.id, amount=300.0, method="card"))
            db.commit()
            context = reply_context.build(db, 5)
        self.assertEqual(context["job"]["invoice"]["balance"], 435.0)

    def test_queue_rows_fragment_shows_draft_and_flag_and_queues_nothing(self):
        response = self.client.get("/dispatch/queue-rows?back=/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["X-Queue-Stamp"])
        self.assertIn('data-queue-item="40"', response.text)
        self.assertIn("waiting on the Fuel injector", response.text)
        self.assertIn("Needs info: parts ETA", response.text)
        self.assertIn("- Jae</textarea>", response.text)
        self.assertIn('data-clean-up="/dispatch/40/clean-up"', response.text)
        again = self.client.get("/dispatch/queue-rows?back=/dashboard")
        self.assertEqual(again.headers["X-Queue-Stamp"], response.headers["X-Queue-Stamp"])
        self.nothing_queued_or_changed()

    def test_clean_up_and_fresh_draft_never_send_or_save(self):
        cleaned = self.client.post("/dispatch/40/clean-up", data={"rough": "tell him the injector is coming thursday"})
        self.assertEqual(cleaned.status_code, 200)
        self.assertEqual(cleaned.json(), {"text": "Hey Testy, the injector is coming thursday.\n- Jae", "source": "rules"})
        fresh = self.client.get("/dispatch/40/draft").json()
        self.assertTrue(fresh["text"].endswith("- Jae"))
        self.assertEqual(fresh["messageId"], "AC40")
        self.assertEqual(self.client.post("/dispatch/40/clean-up", data={"rough": "  "}).status_code, 422)
        self.nothing_queued_or_changed()

    def test_send_still_goes_through_the_owner_tap_only(self):
        rows = self.client.get("/dispatch/queue-rows").text
        self.assertIn('name="decision" value="approve"', rows)
        self.nothing_queued_or_changed()
        sent = self.client.post("/dispatch/40/decide", data={
            "decision": "approve", "expected_message_id": "AC40",
            "reply_text": "Hey Testy, still waiting on the injector.\n- Jae"}, follow_redirects=False)
        self.assertEqual(sent.status_code, 303)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchOutbound).count(), 1)

    def test_inbox_page_polls_the_rows(self):
        page = self.client.get("/dashboard/inbox")
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-queue-refresh="/dispatch/queue-rows?view=open&back=/dashboard/inbox', page.text)
        self.assertIn("/static/reply-desk.js", page.text)


if __name__ == "__main__":
    unittest.main()
