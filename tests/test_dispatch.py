"""Operations queue rules and workflow on a private in-memory database.

All messages are synthetic. Uses its own engine so the shared fixture database
stays empty for test_recovery's startup assertions.
"""
import json
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.dispatch import apply_policies, assess, parse_owner_command
from app.dispatch_model import DispatchAudit, DispatchEvent, DispatchItem, DispatchOutbound, DispatchPolicy, DispatchSync
from app.main import app
from app.models import Customer, User
from app.routes import dispatch, internal_messaging

KEY = "fixture-internal-key"
HEADERS = {"X-Internal-Key": KEY}
INTAKE = "/internal/dispatch/intake"
OWNER = "+16025550199"
AZ = ZoneInfo("America/Phoenix")


def message(ident, body, conversation="CNfixture"):
    return {"conversationId": conversation, "messageId": ident, "phone": "+16025550100", "body": body}


class AssessmentTests(unittest.TestCase):
    def test_safety_cases_go_to_the_owner(self):
        for body in ("2013 Ford Fusion has a fuel leak and flashing check engine light",
                     "2013 Ford Explorer overheating with low oil pressure",
                     "my engine stopped while driving on the freeway"):
            result = assess(body)
            self.assertEqual(result.urgency, "safety", body)
            self.assertIn("high_risk", result.reasons)

    def test_routine_scheduling_needs_no_approval(self):
        result = assess("Can I book an appointment for my 2011 Mitsubishi Eclipse?")
        self.assertEqual((result.category, result.urgency, result.reasons), ("scheduling", "normal", ()))
        self.assertEqual(result.vehicle_text, "2011 Mitsubishi Eclipse")
        self.assertEqual(result.missing, ())

    def test_price_question_requires_verified_estimate(self):
        result = assess("How much for a radiator on a 2005 GMC Yukon?")
        self.assertIn("unverified_estimate", result.reasons)
        self.assertIn("Verify parts and labor", result.proposed_action)

    def test_any_price_question_needs_the_owner(self):
        result = assess("If we wanted to get it towed in 2 weeks how much would towing cost")
        self.assertEqual(result.category, "towing")
        self.assertIn("unverified_estimate", result.reasons)

    def test_missing_details_are_requested_once(self):
        result = assess("I need a tow, it won't move")
        self.assertEqual(result.category, "towing")
        self.assertEqual(result.missing, ("year, make and model", "vehicle location"))

    def test_money_and_legal_language_is_flagged(self):
        self.assertIn("refund", assess("I want my money back").reasons)
        self.assertIn("discount_credit", assess("Can you give me a discount?").reasons)
        self.assertIn("high_risk", assess("I will talk to my lawyer").reasons)

    def test_vin_is_extracted(self):
        self.assertEqual(assess("VIN 1fadp3e25gl254455 please").vin, "1FADP3E25GL254455")

    def test_locked_policies_cannot_be_relaxed(self):
        modes = {"refund": "notify", "high_risk": "notify", "discount_credit": "notify"}
        self.assertEqual(apply_policies(["refund", "high_risk", "discount_credit"], modes), ["refund", "high_risk"])


class OwnerTermsTests(unittest.TestCase):
    def test_firm_words_work_with_or_without_a_number(self):
        for text in ("send it", "go ahead", "yes please", "approve", "Do it."):
            self.assertEqual(parse_owner_command(text), ("approve", None))
        self.assertEqual(parse_owner_command("send 12"), ("approve", 12))
        for text in ("skip", "don't send", "decline"):
            self.assertEqual(parse_owner_command(text), ("decline", None))

    def test_casual_words_need_a_number(self):
        for text in ("ok", "okay", "sure", "yeah", "looks good", "ok thanks", "cancel"):
            self.assertIsNone(parse_owner_command(text))
        self.assertEqual(parse_owner_command("ok 12"), ("approve", 12))
        self.assertEqual(parse_owner_command("nope #12"), ("decline", 12))

    def test_questions_stay_questions(self):
        for text in ("no calls today?", "yes what did he say", "send what", "any task?"):
            self.assertIsNone(parse_owner_command(text))


class QueueWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        self.read_only = app.state.read_only
        app.dependency_overrides[get_db] = override
        patcher = patch.object(internal_messaging, "INTERNAL_API_KEY", KEY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        with self.Session() as db:
            db.add(User(id=1, username="owner", password_hash="x", role="admin"))
            db.add(Customer(id=1, first_name="TEST", last_name="ONLY", phone="602-555-0100"))
            db.commit()
        self.login = patch("app.routes.dispatch.get_current_user",
                           side_effect=lambda request, db: db.get(User, 1))

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def items(self):
        with self.Session() as db:
            return db.query(DispatchItem).order_by(DispatchItem.id).all()

    def post(self, *args):
        return self.client.post(INTAKE, json=message(*args), headers=HEADERS)

    def post_auto(self, *args):
        return self.client.post(INTAKE, json={**message(*args), "autoRepliesOn": True}, headers=HEADERS)

    def test_intake_works_in_read_only_and_requires_the_key(self):
        app.state.read_only = True
        self.assertEqual(self.client.post(INTAKE, json=message("m1", "hi")).status_code, 503)
        self.assertEqual(self.post("m1", "2013 Ford Fusion fuel leak").json()["status"], "needs_owner")
        self.assertEqual(self.items()[0].customer_id, 1)

    def test_reaction_is_not_a_customer_message(self):
        result = self.post("m1", "\u200b👍\u200b to “ Perfect, see you for your appointment Tuesday ”").json()
        self.assertTrue(result["reaction"])
        self.assertEqual(self.items(), [])
        self.assertTrue(self.post("m2", "Loved “See you then”").json()["reaction"])

    def test_arrival_reaches_the_owner_right_away(self):
        response = self.post("m1", "I'm here").json()
        item = self.items()[0]
        self.assertEqual((response["status"], item.urgency, item.category), ("needs_owner", "urgent", "arrival"))
        self.assertNotIn("year, make", item.proposed_action)
        self.assertIsNone(response["autoReply"])

    def test_voicemail_from_the_door_is_urgent(self):
        self.assertEqual(assess("Voicemail: Hi, I'm at your front door").urgency, "urgent")

    def test_upcoming_calendar_event_warns_once(self):
        now = datetime(2026, 11, 3, 9, 50, tzinfo=AZ)
        event = {"start": datetime(2026, 11, 3, 11, 0, tzinfo=AZ), "allDay": False, "title": "Sample inspection", "location": ""}
        with patch.object(dispatch.calendar_feed, "upcoming", return_value=[event]):
            first = [m for m in self.alert(now) if m["key"].startswith("calendar")]
            self.assertIn("11:00 AM: Sample inspection", first[0]["text"])
            self.mark_sent(first[0]["key"])
            self.assertEqual([m for m in self.alert(now) if m["key"].startswith("calendar")], [])

    def test_unmatched_message_is_never_silent(self):
        self.assertEqual(self.post("m1", "sounds good thank you").json()["status"], "needs_owner")

    def test_known_customer_is_never_asked_for_a_vehicle(self):
        body = message("m1", "can I book an appointment")
        body.update(autoRepliesOn=True, knownCustomer=True)
        response = self.client.post(INTAKE, json=body, headers=HEADERS).json()
        self.assertIsNone(response["autoReply"])
        self.assertEqual(response["status"], "needs_owner")

    def test_new_lead_can_still_get_the_vehicle_question(self):
        body = message("m1", "can I book an appointment")
        body.update(autoRepliesOn=True)
        response = self.client.post(INTAKE, json=body, headers=HEADERS).json()
        self.assertEqual(response["status"], "routine")
        self.assertIn("year, make and model", response["autoReply"]["text"])

    def test_reply_in_quo_closes_the_item(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        conversation = self.items()[0].quo_conversation_id
        old = self.client.post("/internal/dispatch/handled", json={"conversationId": conversation, "at": "2020-01-01T00:00:00Z"}, headers=HEADERS)
        self.assertFalse(old.json()["closed"])
        new = self.client.post("/internal/dispatch/handled", json={"conversationId": conversation, "at": "2099-01-01T00:00:00Z"}, headers=HEADERS)
        self.assertTrue(new.json()["closed"])
        self.assertEqual(self.items()[0].status, "closed")

    def test_replayed_message_changes_nothing(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        self.assertTrue(self.post("m1", "2013 Ford Fusion fuel leak").json()["duplicate"])
        self.assertEqual(len(self.items()), 1)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchEvent).count(), 1)

    def test_follow_up_updates_one_item_and_keeps_urgency(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        self.post("m2", "thanks, I'm at 1200 Main St")
        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].urgency, items[0].status, items[0].last_message_id), ("safety", "needs_owner", "m2"))
        self.assertEqual(items[0].vehicle_text, "2013 Ford Fusion")

    def test_routine_policy_choice_is_honored(self):
        with self.Session() as db:
            db.add_all([DispatchPolicy(key="discount_credit", mode="notify"), DispatchPolicy(key="customer_reply", mode="notify")])
            db.commit()
        self.assertEqual(self.post("m1", "any discount for a 2011 Ford Focus?").json()["status"], "routine")

    def test_stale_decision_is_refused_after_a_newer_message(self):
        app.state.read_only = False
        self.post("m1", "2013 Ford Fusion fuel leak")
        self.post("m2", "actually it stopped")
        item = self.items()[0]
        with self.login:
            stale = self.client.post(f"/dispatch/{item.id}/decide", follow_redirects=False,
                                     data={"decision": "approve", "expected_message_id": "m1"})
            fresh = self.client.post(f"/dispatch/{item.id}/decide", follow_redirects=False,
                                     data={"decision": "approve", "expected_message_id": "m2", "proposed_action": "Call them"})
            again = self.client.post(f"/dispatch/{item.id}/decide", follow_redirects=False,
                                     data={"decision": "approve", "expected_message_id": "m2"})
        self.assertEqual((stale.status_code, fresh.status_code, again.status_code), (409, 303, 409))
        item = self.items()[0]
        self.assertEqual((item.status, item.proposed_action, item.decided_by), ("approved", "Call them", 1))

    def test_new_message_after_a_decision_opens_a_new_item(self):
        app.state.read_only = False
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        with self.login:
            self.client.post(f"/dispatch/{item.id}/decide", data={"decision": "decline", "expected_message_id": "m1"})
        self.post("m2", "please call me")
        self.assertEqual([i.status for i in self.items()], ["declined", "needs_owner"])

    def test_nothing_is_sent(self):
        with patch("app.routes.webhooks.send_sms") as send:
            self.post("m1", "2013 Ford Fusion fuel leak")
        send.assert_not_called()

    def test_owner_decisions_need_a_login(self):
        app.state.read_only = False
        self.post("m1", "hello")
        item = self.items()[0]
        response = self.client.post(f"/dispatch/{item.id}/decide", data={"decision": "approve", "expected_message_id": "m1"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.items()[0].status, "needs_owner")

    def test_policy_form_keeps_locked_rules(self):
        app.state.read_only = False
        with patch("app.routes.dispatch.get_current_user", side_effect=lambda request, db: db.get(User, 1)):
            response = self.client.post("/dispatch/policies", follow_redirects=False,
                                        data={"refund": "notify", "discount_credit": "notify"})
        self.assertEqual(response.status_code, 303)
        with self.Session() as db:
            self.assertEqual(db.get(DispatchPolicy, "refund").mode, "approve")
            self.assertEqual(db.get(DispatchPolicy, "discount_credit").mode, "notify")

    def test_actions_are_audited_without_message_text(self):
        app.state.read_only = False
        self.post("m1", "private fuel leak detail on a 2013 Ford Fusion")
        item = self.items()[0]
        with self.login:
            self.client.post(f"/dispatch/{item.id}/decide", data={"decision": "approve", "expected_message_id": "m1"})
            closed = self.client.post(f"/dispatch/{item.id}/close", follow_redirects=False)
        self.assertEqual(closed.status_code, 303)
        with self.Session() as db:
            rows = db.query(DispatchAudit).order_by(DispatchAudit.id).all()
        self.assertEqual([(r.actor, r.action) for r in rows],
                         [("system", "message_received"), ("owner", "owner_approve"), ("owner", "owner_close")])
        self.assertFalse(any("private" in (r.detail or "") for r in rows))

    def test_sync_failures_escalate_and_recover(self):
        app.state.read_only = True
        for _ in range(3):
            self.client.post("/internal/dispatch/sync", headers=HEADERS, json={"source": "quo_recovery", "ok": False})
        with self.Session() as db:
            self.assertTrue(dispatch._sync_health(db)[0]["problem"])
        self.client.post("/internal/dispatch/sync", headers=HEADERS, json={"source": "quo_recovery", "ok": True})
        with self.Session() as db:
            self.assertFalse(dispatch._sync_health(db)[0]["problem"])
            self.assertEqual(db.get(DispatchSync, "quo_recovery").consecutive_failures, 0)

    def test_filters_separate_the_queue(self):
        app.state.read_only = False
        self.post("m1", "2013 Ford Fusion fuel leak", "CNone")
        self.post("m2", "hello there", "CNtwo")
        self.post("m3", "2011 Ford Focus question", "CNthree")
        third = self.items()[2]
        with self.login:
            self.client.post(f"/dispatch/{third.id}/decide", data={"decision": "approve", "expected_message_id": "m3"})
        with self.Session() as db:
            counts = {view: len(dispatch.dashboard_context(db, view)["dispatch_items"]) for view in dispatch.VIEWS}
            self.assertEqual(dispatch.dashboard_context(db, "nonsense")["dispatch_view"], "open")
        self.assertEqual(counts, {"open": 2, "urgent": 1, "approval": 2, "customer": 1, "scheduled": 0, "completed": 0})

    def test_dashboard_renders_cards(self):
        app.state.read_only = True
        self.post("m1", "2013 Ford Fusion fuel leak")
        with patch("app.main.get_current_user", side_effect=lambda request, db: db.get(User, 1)):
            page = self.client.get("/dashboard")
        self.assertEqual(page.status_code, 200)
        self.assertIn("TEST ONLY", page.text)
        self.assertIn("Awaiting approval", page.text)
        self.assertIn("Nothing priced yet", page.text)

    def test_hours_question_gets_one_reply_a_week(self):
        first = self.post_auto("m1", "what are your hours?").json()
        self.assertIn("Suite 508", first["autoReply"]["text"])
        self.assertIn("Mon-Fri 11 AM-7 PM and Sat 12-5 PM", first["autoReply"]["text"])
        self.assertIsNone(self.post_auto("m2", "what are your hours?").json()["autoReply"])

    def test_vehicle_question_is_only_asked_once_and_only_when_missing(self):
        reply = self.post_auto("m1", "I have a noise when I brake").json()["autoReply"]
        self.assertIn("year, make and model", reply["text"])
        self.assertIsNone(self.post_auto("m2", "It's getting worse, there's a noise").json()["autoReply"])
        known = self.post_auto("m3", "2011 Ford Focus has a noise", "CNother").json()
        self.assertIsNone(known["autoReply"])

    def test_sensitive_messages_never_get_an_auto_reply(self):
        for ident, body in enumerate(("Where are you? I have a fuel leak", "what are your hours and how much for brakes?",
                                      "I need a refund, are you open?")):
            result = self.post(f"s{ident}", body, f"CN{ident}").json()
            self.assertIsNone(result["autoReply"], body)

    def test_auto_reply_policy_can_be_turned_off(self):
        with self.Session() as db:
            db.add(DispatchPolicy(key="auto_hours_address", mode="off"))
            db.commit()
        self.assertIsNone(self.post("m1", "what are your hours?").json()["autoReply"])

    def test_recovered_messages_do_not_auto_reply(self):
        response = self.client.post(INTAKE, headers=HEADERS, json={**message("m1", "what are your hours?"), "recovery": True})
        self.assertIsNone(response.json()["autoReply"])

    def owner_text(self, ident, body):
        return self.client.post(INTAKE, headers=HEADERS, json={"conversationId": "CNowner", "messageId": ident, "phone": OWNER, "body": body}).json()

    def alert(self, now=None):
        with self.Session() as db, patch.object(dispatch, "OWNER_PHONE", OWNER):
            return dispatch.due_owner_messages(db, now or datetime(2026, 11, 2, 9, 0, tzinfo=AZ))

    def mark_sent(self, key):
        self.assertEqual(self.client.post("/internal/dispatch/outbound", headers=HEADERS, json={"key": key, "status": "sent"}).status_code, 200)

    def test_approval_text_is_sent_once_and_yes_approves(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        due = self.alert()
        self.assertEqual(len(due), 1)
        self.assertIn(f"Reply YES {item.id}", due[0]["text"])
        self.assertTrue(due[0]["text"].startswith("URGENT"))
        self.mark_sent(due[0]["key"])
        self.assertEqual(self.alert(), [])
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", f"yes {item.id}")
        self.assertTrue(reply["owner"])
        self.assertIn(f"Approved #{item.id}", reply["ownerReply"])
        self.assertEqual(self.items()[0].status, "approved")
        self.assertEqual(len(self.items()), 1)

    def draft(self, item_id, ident, conversation="CNfixture", body="We can take a look today."):
        return self.client.post("/internal/dispatch/draft", headers=HEADERS, json={"conversationId": conversation, "messageId": ident, "body": body}).json()

    def test_alert_shows_the_draft_and_yes_sends_it_once(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        self.assertTrue(self.draft(item.id, "m1")["recorded"])
        due = self.alert()[0]
        self.assertIn("TEST ONLY - 2013 Ford Fusion", due["text"])
        self.assertIn("Draft reply: We can take a look today.", due["text"])
        self.assertIn(f"YES {item.id} sends it", due["text"])
        self.mark_sent(due["key"])
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", f"YES {item.id}")
            again = self.owner_text("o2", f"YES {item.id}")
        self.assertEqual(reply["customerReply"]["text"], "We can take a look today.")
        self.assertEqual(reply["customerReply"]["messageId"], "m1")
        self.assertNotIn("customerReply", again)

    def test_edit_sends_the_owner_version(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        self.draft(item.id, "m1")
        self.mark_sent(self.alert()[0]["key"])
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", f"edit {item.id} Don't drive it, I'll call you in 10.")
        self.assertEqual(reply["customerReply"]["text"], "Don't drive it, I'll call you in 10.")
        self.assertEqual(self.items()[0].status, "approved")

    def test_no_sends_nothing_and_stale_drafts_are_dropped(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        self.draft(item.id, "m1")
        self.mark_sent(self.alert()[0]["key"])
        self.post("m2", "it got worse")
        self.assertIsNone(self.items()[0].draft_reply)
        self.assertFalse(self.draft(item.id, "m1")["recorded"])
        self.draft(item.id, "m2")
        self.mark_sent(self.alert()[0]["key"])
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", f"NO {item.id}")
        self.assertNotIn("customerReply", reply)
        self.assertIn("Nothing was sent", reply["ownerReply"])

    def test_non_urgent_alert_waits_briefly_for_its_draft(self):
        self.post("m1", "How much for brakes on a 2011 Ford Focus?")
        alerts = lambda: [m for m in self.alert(datetime.now(AZ)) if m["key"].startswith("alert")]
        self.assertEqual(alerts(), [])
        self.draft(1, "m1")
        self.assertIn("Draft reply", alerts()[0]["text"])

    def test_contact_label_shows_in_alerts_and_is_not_repeated(self):
        data = {**message("m1", "2011 Mitsubishi Eclipse has a fuel leak", "CNlabel"), "phone": "+16025550111", "contactName": "2011 Mitsubishi Eclipse Inspection"}
        self.client.post(INTAKE, headers=HEADERS, json=data)
        text = self.alert()[0]["text"]
        self.assertTrue(text.startswith("URGENT #1: 2011 Mitsubishi Eclipse Inspection\n"))

    def test_intake_reports_details_for_contact_saving(self):
        result = self.post("m1", "2013 Ford Fusion fuel leak").json()
        self.assertEqual((result["vehicleText"], result["category"]), ("2013 Ford Fusion", "repair"))

    def test_missed_call_needs_the_owner_and_yes_checks_the_text_thread(self):
        data = {**message("call1", "Voicemail: please call me"), "kind": "call", "threadMessageId": "text9", "recovery": True}
        result = self.client.post(INTAKE, headers=HEADERS, json=data).json()
        self.assertEqual(result["status"], "needs_owner")
        self.assertIsNone(result["autoReply"])
        item = self.items()[0]
        self.draft(item.id, "call1")
        self.mark_sent(self.alert()[0]["key"])
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", f"YES {item.id}")
        self.assertEqual(reply["customerReply"]["messageId"], "text9")

    def test_replayed_message_labels_an_older_item(self):
        data = {**message("m1", "hello", "CNold"), "phone": "+16025550111"}
        self.client.post(INTAKE, headers=HEADERS, json=data)
        self.client.post(INTAKE, headers=HEADERS, json={**data, "contactName": "Melo Pontiac G6"})
        self.assertEqual(self.items()[0].contact_name, "Melo Pontiac G6")

    def test_yes_is_refused_after_a_newer_customer_message(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        self.mark_sent(self.alert()[0]["key"])
        self.post("m2", "it's on fire now")
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", f"YES {item.id}")
        self.assertIn("newer customer message", reply["ownerReply"])
        self.assertEqual(self.items()[0].status, "needs_owner")
        self.assertEqual(len(self.alert()), 1)  # the newer message alerts again

    def test_only_the_owner_number_can_decide(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        item = self.items()[0]
        self.mark_sent(self.alert()[0]["key"])
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            self.post("m2", f"YES {item.id}", "CNstranger")
        self.assertEqual(self.items()[0].status, "needs_owner")

    def test_failed_owner_text_is_offered_again(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        key = self.alert()[0]["key"]
        self.client.post("/internal/dispatch/outbound", headers=HEADERS, json={"key": key, "status": "failed"})
        self.assertEqual(self.alert()[0]["key"], key)

    def test_daily_summary_runs_once_at_ten_and_not_on_sunday(self):
        monday = datetime(2026, 11, 2, 10, 5, tzinfo=AZ)
        due = self.alert(monday)
        self.assertEqual([m["key"] for m in due], ["daily:2026-11-02"])
        self.assertIn("App bookings today: 0", due[0]["text"])
        self.assertEqual(self.alert(monday), [])
        self.assertEqual(self.alert(datetime(2026, 11, 1, 10, 5, tzinfo=AZ)), [])

    def test_stale_items_get_one_nudge_a_day(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        self.mark_sent(self.alert()[0]["key"])
        with self.Session() as db:
            row = db.query(DispatchItem).one()
            row.updated_at = datetime(2026, 11, 3, 8, 0, tzinfo=AZ)
            db.commit()
        late = datetime(2026, 11, 3, 16, 0, tzinfo=AZ)
        nudges = [m for m in self.alert(late) if m["key"].startswith("nudge")]
        self.assertEqual(len(nudges), 1)
        self.assertIn("Still open (1)", nudges[0]["text"])
        self.assertNotIn("year, make", nudges[0]["text"])
        self.assertEqual([m for m in self.alert(late) if m["key"].startswith("nudge")], [])
        self.assertEqual([m for m in self.alert(datetime(2026, 11, 4, 21, 0, tzinfo=AZ)) if m["key"].startswith("nudge")], [])

    def test_a_question_gets_a_read_only_snapshot(self):
        self.post("m1", "2013 Ford Fusion fuel leak")
        with patch.object(dispatch, "OWNER_PHONE", OWNER):
            reply = self.owner_text("o1", "any task?")
        self.assertEqual(reply["ownerQuestion"], "any task?")
        self.assertEqual(reply["snapshot"]["waitingOnYou"][0]["id"], 1)
        self.assertNotIn("customerReply", reply)
        self.assertEqual(self.items()[0].status, "needs_owner")

    def test_owner_commands_parse(self):
        self.assertEqual(parse_owner_command("Yes 12"), ("approve", 12))
        self.assertEqual(parse_owner_command("no #7."), ("decline", 7))
        self.assertEqual(parse_owner_command("y"), ("approve", None))
        self.assertEqual(parse_owner_command("OPEN"), ("list", None))
        self.assertEqual(parse_owner_command("Edit #4 On my way"), ("edit", 4, "On my way"))
        self.assertIsNone(parse_owner_command("yes I think so but call them"))

    def test_reasons_are_stored_as_json(self):
        self.post("m1", "How much for brakes on a 2011 Ford Focus?")
        self.assertEqual(json.loads(self.items()[0].approval_reasons), ["unverified_estimate"])


if __name__ == "__main__":
    unittest.main()
