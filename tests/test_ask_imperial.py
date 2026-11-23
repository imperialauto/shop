"""Ask Imperial: three actions, the Local Node contract, stale drafts. Synthetic records only;
no Ollama, Quo or customer contact."""
import json
import os
import unittest
from datetime import timedelta
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import assist_guard, diagnostic_guard
from app.assist_model import AssistEvent, AssistRequest, LaborEvidence, ShopRule
from app.auth import hash_password
from app.database import Base, get_db
from app.dispatch_model import DispatchItem
from app.imperial_task_model import ImperialTask
from app.local_node_model import LocalNodeHeartbeat
from app.main import app
from app.models import Customer, LineItem, RepairOrder, User, Vehicle
from app.permissions import allowed
from app.routes import internal_messaging

PASSWORD = "fixture-password-1"
NODE_KEY = "n" * 48
NODE = {"X-Local-Node-Key": NODE_KEY}
INTERNAL = {"X-Internal-Key": "i" * 48}
READING = {"operations": [{"name": "Brake pads and rotors, front, replace", "system": "brakes", "position": "front"}],
           "vehicle_details_to_confirm": ["Brake package"], "parts_to_price": ["front brake pads", "front rotors"],
           "notes_for_advisor": []}


class GuardTests(unittest.TestCase):
    def test_reading_passes_and_numbers_are_refused(self):
        self.assertTrue(assist_guard.validate(json.dumps(READING))["ok"])
        for bad, why in ((["Brake pads 1.6 hours"], "labor_time_mentioned"), (["Pads about $89"], "price_mentioned"),
                         (["Use part number BR-12345"], "part_number_mentioned"),
                         (["Customer approved the brakes"], "approval_claimed")):
            verdict = assist_guard.validate(json.dumps({**READING, "notes_for_advisor": bad}))
            self.assertFalse(verdict["ok"])
            self.assertIn(why, verdict["violations"])
        self.assertEqual(assist_guard.validate("not json")["violations"], ["not_json"])

    def test_diagnostic_validator_still_rejects_labor_and_price(self):
        snapshot = {"job_reference": "RO-1", "vehicle": {}, "customer_reported_concern": "noise",
                    "technician_notes": "", "technician_readings": "", "reported_codes": []}
        draft = {k: [] for k in diagnostic_guard.LIST_KEYS}
        draft["missing_information"] = ["Labor is 1.6 hours at $150"]
        verdict = diagnostic_guard.validate_output(json.dumps(draft), snapshot)
        self.assertFalse(verdict["ok"])

    def test_snapshot_has_no_customer_contact(self):
        class V:  # noqa: D401
            year, make, model, engine = 2022, "Dodge", "Charger", "5.7L"
        class R:
            id, vehicle, concern, line_items = 9, V(), "Call 602-555-0100 or a@b.com", []
        snap = assist_guard.build_snapshot(R(), "estimate brakes for 602 555 0100", ["Rotors with every pad job"])
        blob = json.dumps(snap)
        self.assertNotIn("555", blob)
        self.assertNotIn("a@b.com", blob)


class AskImperialTests(unittest.TestCase):
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
        self.patches = [patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": NODE_KEY,
                                                "ASK_IMPERIAL_ENABLED": "true", "IMPERIAL_TASKS_ENABLED": "true"}),
                        patch.object(internal_messaging, "INTERNAL_API_KEY", "i" * 48)]
        for p in self.patches:
            p.start()
        with self.Session() as db:
            for uid, name, role in ((1, "owner", "owner"), (2, "crew", "tech"), (3, "desk", "office")):
                db.add(User(id=uid, username=name, full_name=name.title(), password_hash=hash_password(PASSWORD), role=role))
            db.add(Customer(id=1, first_name="Synthetic", last_name="Two-Cars", phone="6025550142"))
            db.add(Vehicle(id=1, customer_id=1, year=2022, make="Dodge", model="Charger", engine="5.7L"))
            db.add(Vehicle(id=2, customer_id=1, year=2016, make="Ford", model="Focus"))
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="diagnosing", concern="Brakes grinding"))
            db.add(RepairOrder(id=2, customer_id=1, vehicle_id=2, status="diagnosing", concern="Shudder"))
            db.commit()
        self.worker = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        for p in self.patches:
            p.stop()
        self.engine.dispose()

    def staff(self, username):
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.post("/login", data={"username": username, "password": PASSWORD}).status_code, 302)
        return client

    def beat(self, worker="shop-pc", version="1.2.0"):
        self.assertEqual(self.worker.post("/internal/local-node/heartbeat", headers=NODE, json={
            "workerId": worker, "version": version, "model": "m", "ollamaOk": True}).status_code, 200)

    def claim(self, worker="shop-pc"):
        return self.worker.post("/internal/local-node/claim", headers=NODE, json={"workerId": worker}).json()["task"]

    def result(self, task, worker="shop-pc", output=None):
        return self.worker.post(task["resultPath"] and f"/internal/local-node{task['resultPath']}", headers=NODE, json={
            "workerId": worker, "leaseToken": task["leaseToken"], "output": json.dumps(output or READING)})

    def start_estimate(self, client, text="Estimate front brakes on the Charger", ro=1):
        response = client.post("/ask", data={"text": text, "ro": str(ro)})
        self.assertEqual(response.status_code, 303, response.text[:300])
        return int(response.headers["location"].rsplit("/", 1)[1])

    def row(self, request_id):
        with self.Session() as db:
            return db.get(AssistRequest, request_id)

    def advisor_tasks(self):
        with self.Session() as db:
            return db.query(ImperialTask).filter(ImperialTask.source == "ask_imperial").all()

    def evidence(self, client, request_id, hours="1.60", price="89.99"):
        updated = self.row(request_id).updated_at.isoformat()
        return client.post(f"/ask/requests/{request_id}/evidence", data={
            "updated": updated, "source": "ALLDATA Repair", "verified": "yes", "vehicle_config": "5.7L RWD, standard brakes",
            "operation": "Brake pads and rotors, front, replace", "guide_hours": hours, "rate_type": "shop",
            "inclusions": "Includes rotor replacement", "overlap": "", "shop_manager_ref": "SM-4471",
            "part_description_0": "Front pads", "part_vendor_0": "AutoZonePro", "part_part_number_0": "", "part_price_0": price,
            "part_qty_0": "1", "part_availability_0": "In stock", "save_as": "job", "rule_text": "",
            "part_description_1": "front rotors", "part_vendor_1": "AutoZonePro", "part_price_1": ""})  # suggestion, not priced

    # -- entry points and roles -------------------------------------------
    def test_button_everywhere_with_job_context_and_off_switch(self):
        owner = self.staff("owner")
        page = owner.get("/ro/1").text
        self.assertIn('href="/ask?ro=1" class="ask-desk"', page)
        self.assertIn('href="/ask?ro=1" class="ask-top"', page)
        self.assertIn('href="/ask" class="ask-desk"', owner.get("/dashboard/tasks").text)
        ask = owner.get("/ask?ro=1").text
        self.assertIn("RO #1", ask)
        self.assertIn('name="ro" value="1"', ask)
        with patch.dict(os.environ, {"ASK_IMPERIAL_ENABLED": ""}):
            self.assertNotIn("class=\"ask-desk\"", owner.get("/ro/1").text)
            self.assertEqual(owner.get("/ask").status_code, 503)

    def test_permissions(self):
        self.assertTrue(allowed("tech", "GET", "/ask"))
        self.assertTrue(allowed("tech", "POST", "/ask/note"))
        self.assertFalse(allowed("tech", "POST", "/ask/requests/1/apply"))
        self.assertFalse(allowed("tech", "POST", "/ask/task"))
        self.assertTrue(allowed("office", "POST", "/ask/requests/1/apply"))
        crew = self.staff("crew")
        page = crew.post("/ask", data={"text": "Estimate front brakes", "ro": "1"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("for the office, admin or owner", page.text)

    # -- action 1: find a job ---------------------------------------------
    def test_find_job_and_next_step(self):
        owner = self.staff("owner")
        page = owner.post("/ask", data={"text": "What should I do next on RO 2?"}).text
        self.assertIn("RO #2", page)
        self.assertIn("Next:", page)
        self.assertIn('href="/ro/2#next-step"', page)
        both = owner.post("/ask", data={"text": "RO 1 or RO 2?"}).text
        self.assertIn("Which job do you mean?", both)
        self.assertIn("Nothing was changed.", both)

    # -- action 2: note and task ------------------------------------------
    def test_note_preview_then_apply_links_the_record(self):
        crew = self.staff("crew")
        preview = crew.post("/ask", data={"text": "note: rotors measured under spec", "ro": "1"}).text
        self.assertIn("Check before saving", preview)
        self.assertIn("Add internal note: “rotors measured under spec”", preview)
        self.assertIn("2022 Dodge Charger", preview)
        with self.Session() as db:
            self.assertFalse(db.get(RepairOrder, 1).tech_notes)  # nothing saved by the preview
        saved = crew.post("/ask/note", data={"ro": "1", "note": "rotors measured under spec"})
        self.assertEqual(saved.headers["location"], "/ask?ro=1&saved=/ro/1#notes")
        with self.Session() as db:
            self.assertIn("Ask Imperial] rotors measured under spec", db.get(RepairOrder, 1).tech_notes)
            self.assertEqual(db.query(AssistEvent).filter(AssistEvent.action == "note_added").one().repair_order_id, 1)

    def test_task_update_is_previewed_and_owner_only(self):
        with self.Session() as db:
            db.add(ImperialTask(id=118, source="dashboard", external_id="t118", title="Order tank", category="general"))
            db.commit()
        owner = self.staff("owner")
        preview = owner.post("/ask", data={"text": "task 118 done"}).text
        self.assertIn("Mark done: task #118 “Order tank”", preview)
        self.assertEqual(owner.post("/ask/task", data={"task_id": "118", "operation": "complete"}).status_code, 303)
        with self.Session() as db:
            self.assertEqual(db.get(ImperialTask, 118).status, "done_verified")
        desk = self.staff("desk")
        self.assertIn("Only the owner", desk.post("/ask", data={"text": "task 118 done"}).text)

    # -- action 3: estimate draft, happy path ------------------------------
    def test_estimate_draft_end_to_end(self):
        desk = self.staff("desk")
        request_id = self.start_estimate(desk)
        self.assertEqual(self.start_estimate(desk), request_id)  # a second tap reuses the open draft
        self.beat(version="1.1.0")
        self.assertIsNone(self.claim())  # old workers never get Ask Imperial work
        self.beat()
        task = self.claim()
        self.assertEqual((task["taskType"], task["resultPath"]), ("assist_estimate", f"/assist/{request_id}/result"))
        self.assertNotIn("5550142", json.dumps(task["messages"]))
        # The AI may not price: an answer with hours is refused and retried
        refused = self.result(task, output={**READING, "notes_for_advisor": ["about 1.6 hours"]}).json()
        self.assertEqual((refused["accepted"], refused["status"]), (False, "retry_wait"))
        with self.Session() as db:
            db.get(AssistRequest, request_id).retry_after = db.get(AssistRequest, request_id).created_at
            db.commit()
        task = self.claim()
        self.assertEqual(self.result(task).json(), {"accepted": True, "status": "needs_labor"})
        page = desk.get(f"/ask/requests/{request_id}").text
        self.assertIn("Needs labor lookup", page)
        self.assertIn("Brake pads and rotors, front, replace", page)
        self.assertEqual(len(self.advisor_tasks()), 1)
        self.assertEqual(self.evidence(desk, request_id).status_code, 303)
        page = desk.get(f"/ask/requests/{request_id}").text
        self.assertIn("ALLDATA Repair", page)
        self.assertIn("SM-4471", page)
        # 1.60 h x $150 = 240.00; pads 89.99 + 9.2% tax 8.28 -> 338.27
        self.assertIn("$240.00", page)
        self.assertIn("$8.28", page)
        self.assertIn("$338.27", page)
        updated = self.row(request_id).updated_at.isoformat()
        applied = desk.post(f"/ask/requests/{request_id}/apply", data={"updated": updated})
        self.assertEqual(applied.status_code, 303)
        with self.Session() as db:
            lines = db.query(LineItem).filter(LineItem.repair_order_id == 1).all()
            self.assertEqual([(l.item_type, l.quantity, l.unit_price, l.approval) for l in lines],
                             [("labor", 1.6, 150.0, None), ("part", 1.0, 89.99, None)])
            self.assertIn("ALLDATA Repair: Brake pads and rotors, front, replace", lines[0].description)
            self.assertEqual(db.get(AssistRequest, request_id).status, "applied")
        self.assertEqual([t.status for t in self.advisor_tasks()], ["done_verified"])
        self.assertIn("Open RO #1", desk.get(f"/ask/requests/{request_id}").text)

    def test_shop_rule_saved_only_when_chosen(self):
        desk = self.staff("desk")
        request_id = self.start_estimate(desk)
        self.evidence(desk, request_id)  # "This job only"
        with self.Session() as db:
            self.assertEqual(db.query(ShopRule).count(), 0)
        updated = self.row(request_id).updated_at.isoformat()
        desk.post(f"/ask/requests/{request_id}/evidence", data={
            "updated": updated, "source": "ALLDATA Repair", "verified": "yes", "vehicle_config": "5.7L",
            "operation": "Brake pads and rotors, front, replace", "guide_hours": "1.6", "rate_type": "shop",
            "save_as": "shop_rule", "rule_text": "Quote rotors with every pad job on Chargers"})
        with self.Session() as db:
            self.assertEqual([r.text for r in db.query(ShopRule)], ["Quote rotors with every pad job on Chargers"])
            self.assertEqual(db.query(LaborEvidence).filter(LaborEvidence.superseded_by_id.is_(None)).count(), 1)

    def test_labor_unavailable_and_workstation_offline(self):
        desk = self.staff("desk")
        request_id = self.start_estimate(desk)
        page = desk.get(f"/ask/requests/{request_id}").text
        self.assertIn("Workstation offline", page)  # never checked in
        self.assertIn("Needs labor lookup", page)
        self.assertNotIn("Apply to RO #1", page)
        # No evidence -> no price and no Apply
        self.assertEqual(desk.post(f"/ask/requests/{request_id}/apply",
                                   data={"updated": self.row(request_id).updated_at.isoformat()}).status_code, 303)
        self.assertEqual(self.row(request_id).status, "queued")
        # Advisor can enter labor by hand while the PC is off; the AI attempt is dropped
        self.evidence(desk, request_id)
        self.assertEqual(self.row(request_id).status, "ready")
        self.beat()
        self.assertIsNone(self.claim())


class StaleDraftTests(AskImperialTests):
    """Handoff part 3 at unit level; tests/e2e/ask_imperial_restart.py runs it with a real worker process."""

    def test_scope_change_and_hold_off_after_claim(self):
        desk = self.staff("desk")
        request_id = self.start_estimate(desk)
        self.beat("shop-pc-a")
        old = self.claim("shop-pc-a")
        # Scope changes on RO 1 and the customer (who owns both vehicles) asks to hold off
        with self.Session() as db:
            db.add(LineItem(repair_order_id=1, description="Brake fluid flush", quantity=1, unit_price=99))
            db.commit()
        hold = {"conversationId": "CNqa", "messageId": "MSG-HOLD-1", "phone": "+16025550142",
                "body": "Please hold off on the brakes for now", "knownCustomer": True}
        for _ in range(2):  # the message is replayed
            self.assertEqual(self.worker.post("/internal/dispatch/intake", headers=INTERNAL, json=hold).status_code, 200)
        with self.Session() as db:
            self.assertEqual(db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == "CNqa").count(), 1)
            # Worker restarted: its lease runs out and a fresh worker process reclaims it
            row = db.get(AssistRequest, request_id)
            row.lease_until = row.lease_until - timedelta(hours=1)
            db.commit()
        self.beat("shop-pc-b")
        new = self.claim("shop-pc-b")
        self.assertEqual(new["taskId"], request_id)
        self.assertEqual(self.result(new, "shop-pc-b").json(), {"accepted": True, "status": "stale"})
        # The old worker's late result is refused and recorded; it changes nothing
        late = self.result(old, "shop-pc-a", output={**READING, "operations": [{"name": "Wrong", "system": "", "position": ""}]})
        self.assertEqual(late.status_code, 409)
        row = self.row(request_id)
        self.assertEqual((row.repair_order_id, row.status), (1, "stale"))
        reasons = json.loads(row.stale_reason)
        self.assertIn("Line added: “Brake fluid flush”", reasons)
        self.assertTrue(any("hold off on the brakes" in r for r in reasons))
        self.assertIn("Brake pads and rotors", row.ai_json)
        with self.Session() as db:
            actions = [e.action for e in db.query(AssistEvent).filter(AssistEvent.request_id == request_id)]
        self.assertIn("late_result_refused", actions)
        # Stale Apply and stale Send are refused
        refused = desk.post(f"/ask/requests/{request_id}/apply", data={"updated": row.updated_at.isoformat()})
        self.assertIn("error=", refused.headers["location"])
        self.assertEqual(desk.post("/ro/1/request-approval").status_code, 409)
        # The owner's direct sends (Request approval, Text link now) are refused the same way
        owner = self.staff("owner")
        self.assertEqual(owner.post("/ro/1/request-approval").status_code, 409)
        self.assertEqual(owner.post("/ro/1/text-link-now").status_code, 409)
        # Exactly one actionable advisor item
        self.assertEqual(len([t for t in self.advisor_tasks() if t.status == "open"]), 1)
        # Same screen: revise, labor verified, apply
        self.assertEqual(desk.post(f"/ask/requests/{request_id}/revise").status_code, 303)
        self.assertEqual(self.row(request_id).status, "needs_labor")
        self.evidence(desk, request_id)
        applied = desk.post(f"/ask/requests/{request_id}/apply",
                            data={"updated": self.row(request_id).updated_at.isoformat()})
        self.assertNotIn("error=", applied.headers["location"])
        self.assertEqual(self.row(request_id).status, "applied")
        self.assertEqual([t.status for t in self.advisor_tasks()], ["done_verified"])
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 1).id, self.row(request_id).repair_order_id)
            self.assertEqual(db.query(LineItem).filter(LineItem.repair_order_id == 2).count(), 0)  # other car untouched

    def test_evidence_kept_when_draft_goes_stale_after_pricing(self):
        desk = self.staff("desk")
        request_id = self.start_estimate(desk)
        self.evidence(desk, request_id)
        with self.Session() as db:
            db.get(RepairOrder, 1).concern = "Brakes grinding, also a pull to the left"
            db.commit()
        refused = desk.post(f"/ask/requests/{request_id}/apply", data={"updated": self.row(request_id).updated_at.isoformat()})
        self.assertIn("concern", refused.headers["location"])
        self.assertEqual(self.row(request_id).status, "stale")
        desk.post(f"/ask/requests/{request_id}/revise")
        self.assertEqual(self.row(request_id).status, "ready")  # verified evidence kept
        page = desk.get(f"/ask/requests/{request_id}").text
        self.assertIn("SM-4471", page)
        self.assertIn("Apply to RO #1", page)


if __name__ == "__main__":
    unittest.main()
