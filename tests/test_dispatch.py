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

    def test_intake_works_in_read_only_and_requires_the_key(self):
        app.state.read_only = True
        self.assertEqual(self.client.post(INTAKE, json=message("m1", "hi")).status_code, 503)
        self.assertEqual(self.post("m1", "2013 Ford Fusion fuel leak").json()["status"], "needs_owner")
        self.assertEqual(self.items()[0].customer_id, 1)

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
            db.add(DispatchPolicy(key="discount_credit", mode="notify"))
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
        self.assertEqual([i.status for i in self.items()], ["declined", "routine"])

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
        self.assertEqual(self.items()[0].status, "routine")

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
        self.assertEqual(counts, {"open": 2, "urgent": 1, "approval": 1, "customer": 1, "scheduled": 0, "completed": 0})

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
        first = self.post("m1", "what are your hours?").json()
        self.assertIn("Suite 508", first["autoReply"]["text"])
        self.assertIn("Mon-Fri 11 AM-7 PM and Sat 12-5 PM", first["autoReply"]["text"])
        self.assertIsNone(self.post("m2", "what are your hours?").json()["autoReply"])

    def test_vehicle_question_is_only_asked_once_and_only_when_missing(self):
        reply = self.post("m1", "I have a noise when I brake").json()["autoReply"]
        self.assertIn("year, make and model", reply["text"])
        self.assertIsNone(self.post("m2", "It's getting worse, there's a noise").json()["autoReply"])
        known = self.post("m3", "2011 Ford Focus has a noise", "CNother").json()
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
        self.assertIn("Today: 0 booked", due[0]["text"])
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
        self.assertIn("Still waiting on you", nudges[0]["text"])
        self.assertEqual([m for m in self.alert(late) if m["key"].startswith("nudge")], [])
        self.assertEqual([m for m in self.alert(datetime(2026, 11, 4, 21, 0, tzinfo=AZ)) if m["key"].startswith("nudge")], [])

    def test_owner_commands_parse(self):
        self.assertEqual(parse_owner_command("Yes 12"), ("approve", 12))
        self.assertEqual(parse_owner_command("no #7."), ("decline", 7))
        self.assertEqual(parse_owner_command("y"), ("approve", None))
        self.assertEqual(parse_owner_command("OPEN"), ("list", None))
        self.assertIsNone(parse_owner_command("yes I think so but call them"))

    def test_reasons_are_stored_as_json(self):
        self.post("m1", "How much for brakes on a 2011 Ford Focus?")
        self.assertEqual(json.loads(self.items()[0].approval_reasons), ["unverified_estimate"])


if __name__ == "__main__":
    unittest.main()
