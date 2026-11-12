"""Imperial Local Node: synthetic jobs only. No Ollama, Railway or customer data."""
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import diagnostic_guard as guard
from app.auth import hash_password
from app.database import Base, get_db
from app.local_node_model import DiagnosticReview, DiagnosticReviewEvent, LocalNodeHeartbeat
from app.main import app
from app.models import Customer, RepairOrder, User, Vehicle
from app.permissions import allowed
from app.routes import internal_messaging, local_node

PASSWORD = "fixture-password-1"
NODE_KEY = "n" * 48
NODE = {"X-Local-Node-Key": NODE_KEY}


def good_output(**changes):
    draft = {
        "customer_reported_symptoms": ["Check engine light on", "Engine shakes at idle"],
        "reported_codes": [{"code": "P0301", "status_as_supplied": "not supplied",
                            "meaning": "Cylinder 1 misfire detected"}],
        "technician_confirmed_findings": [],
        "suspected_causes": [{"cause": "Ignition coil fault on cylinder 1", "basis": "P0301 and rough idle complaint",
                              "likelihood": "medium"}],
        "recommended_tests": [{"test": "Swap coil 1 with another cylinder and recheck misfire counters",
                               "purpose": "See whether the misfire follows the coil"}],
        "proposed_repairs": [],
        "missing_information": ["Status of P0301 (active, pending or stored)", "Freeze frame data"],
        "cautions": [],
    }
    draft.update(changes)
    return draft


SNAPSHOT = {"job_reference": "RO-1", "vehicle": {"year": 2014, "make": "Ford", "model": "F-150", "engine": "3.5L",
                                                  "mileage": 128400},
            "customer_reported_concern": "Check engine light, shakes at idle", "technician_notes": "",
            "technician_readings": "", "reported_codes": [{"code": "P0301", "status": "not supplied"}]}


class GuardTests(unittest.TestCase):
    def check(self, draft, snapshot=SNAPSHOT):
        return guard.validate_output(json.dumps(draft), snapshot)

    def test_parse_codes_never_assumes_status(self):
        codes, problems = guard.parse_codes("P0301\np0171 pending\nP0420 - history, U0100 active\nP0301 active")
        self.assertEqual(problems, [])
        self.assertEqual(codes, [{"code": "P0301", "status": "not supplied"}, {"code": "P0171", "status": "pending"},
                                 {"code": "P0420", "status": "stored"}, {"code": "U0100", "status": "active"}])
        self.assertTrue(guard.parse_codes("misfire")[1])

    def test_normal_draft_passes_and_reminds_status_missing(self):
        verdict = self.check(good_output())
        self.assertTrue(verdict["ok"], verdict["violations"])
        self.assertTrue(any("without a status" in c for c in verdict["cautions"]))

    def test_p0301_described_active_without_status_is_rejected(self):
        draft = good_output(suspected_causes=[{"cause": "Coil failure", "basis": "P0301 is active",
                                               "likelihood": "high"}])
        self.assertIn("code_status_asserted:P0301", self.check(draft)["violations"])
        draft = good_output(reported_codes=[{"code": "P0301", "status_as_supplied": "active", "meaning": "misfire"}])
        self.assertIn("code_status_changed:P0301:active", self.check(draft)["violations"])
        active_snap = {**SNAPSHOT, "reported_codes": [{"code": "P0301", "status": "active"}]}
        for synonym, ok in (("Active", True), ("current", True), ("confirmed", True), ("stored", False),
                            ("active (misfire on cylinder 1)", False), ("", False)):
            draft = good_output(reported_codes=[{"code": "P0301", "status_as_supplied": synonym, "meaning": "misfire"}])
            self.assertEqual(self.check(draft, active_snap)["ok"], ok, synonym)
        hedged = good_output(missing_information=["Confirm whether P0301 is active or stored"])
        self.assertTrue(self.check(hedged)["ok"])

    def test_fabricated_codes_rejected(self):
        draft = good_output(suspected_causes=[{"cause": "Lean condition", "basis": "P0171 likely", "likelihood": "low"}])
        self.assertIn("fabricated_code:P0171", self.check(draft)["violations"])

    def test_missing_information_case(self):
        snap = {**SNAPSHOT, "vehicle": {"year": None, "make": None, "model": None, "engine": None, "mileage": None},
                "customer_reported_concern": "Truck is making a noise", "reported_codes": []}
        draft = good_output(customer_reported_symptoms=["Noise"], reported_codes=[], suspected_causes=[],
                            recommended_tests=[{"test": "Road test with technician", "purpose": "Locate the noise"}],
                            missing_information=["Year, make, model", "When the noise happens"])
        verdict = self.check(draft, snap)
        self.assertTrue(verdict["ok"], verdict["violations"])
        invented = good_output(customer_reported_symptoms=["Noise from the 2018 Ram engine bay"], reported_codes=[],
                               suspected_causes=[])
        self.assertIn("unsupported_year:2018", self.check(invented, snap)["violations"])

    def test_unsupported_repairs_rejected(self):
        code_only = good_output(proposed_repairs=[{"repair": "Replace ignition coil 1",
                                                   "justified_by_confirmed_findings": ["P0301"]}])
        violations = self.check(code_only)["violations"]
        self.assertIn("repair_without_confirmed_finding", violations)
        self.assertIn("repair_justified_by_code_only", violations)
        made_up = good_output(technician_confirmed_findings=["Coil 1 is bad"])
        self.assertIn("confirmed_findings_without_technician_input", self.check(made_up)["violations"])

    def test_repair_backed_by_technician_finding_passes(self):
        snap = {**SNAPSHOT, "technician_notes": "Swapped coil 1 and 3; misfire moved to cylinder 3.",
                "reported_codes": [{"code": "P0301", "status": "active"}]}
        draft = good_output(
            reported_codes=[{"code": "P0301", "status_as_supplied": "active", "meaning": "Cylinder 1 misfire"}],
            technician_confirmed_findings=["Misfire followed coil 1 to cylinder 3 after swap"],
            proposed_repairs=[{"repair": "Replace the original cylinder 1 ignition coil",
                               "justified_by_confirmed_findings": ["Misfire followed coil 1 to cylinder 3 after swap"]}],
            missing_information=[])
        verdict = self.check(draft, snap)
        self.assertTrue(verdict["ok"], verdict["violations"])

    def test_prices_labor_part_numbers_readings_and_approvals_rejected(self):
        cases = {
            "price_mentioned": good_output(cautions=["Coil costs about $85"]),
            "labor_time_mentioned": good_output(cautions=["Book labor is 1.2 hours"]),
            "part_number_mentioned": good_output(cautions=["Use part number DG-508"]),
            "unsupported_reading:42psi": good_output(cautions=["Fuel pressure was 42 psi"]),
            "approval_claimed": good_output(cautions=["Customer approved the coil replacement"]),
        }
        for expected, draft in cases.items():
            self.assertIn(expected, self.check(draft)["violations"], expected)

    def test_invalid_output_shapes(self):
        self.assertEqual(guard.validate_output("Sure! The coil is bad.", SNAPSHOT)["violations"], ["not_json"])
        self.assertEqual(guard.validate_output("[]", SNAPSHOT)["violations"], ["not_an_object"])
        partial = good_output()
        del partial["recommended_tests"]
        self.assertIn("missing_field:recommended_tests", self.check(partial)["violations"])
        wrapped = "<think>hmm</think>```json\n" + json.dumps(good_output()) + "\n```"
        self.assertTrue(guard.validate_output(wrapped, SNAPSHOT)["ok"])

    def test_text_rendering_keeps_sections_separate(self):
        rendered = guard.draft_to_text(guard.validate_output(json.dumps(good_output()), SNAPSHOT)["draft"])
        for heading in ("Customer-reported symptoms:", "Technician-confirmed findings:",
                        "Suspected causes (NOT confirmed):", "Recommended tests:", "Proposed repairs"):
            self.assertIn(heading, rendered)
        self.assertIn("P0301 — status: not supplied", rendered)
        blank = good_output(reported_codes=[{"code": "P0301", "status_as_supplied": "not supplied", "meaning": ""}])
        rendered = guard.draft_to_text(guard.validate_output(json.dumps(blank), SNAPSHOT)["draft"])
        self.assertIn("- P0301 — status: not supplied\n", rendered)  # no dangling dash when meaning is blank


class PermissionTableTests(unittest.TestCase):
    def test_tech_and_office_reach_reviews(self):
        for role in ("tech", "office", "admin"):
            self.assertTrue(allowed(role, "GET", "/ro/4/ai-review/2"), role)
            self.assertTrue(allowed(role, "GET", "/ro/4/ai-review/2/status"), role)
            self.assertTrue(allowed(role, "POST", "/ro/4/ai-review"), role)
            self.assertTrue(allowed(role, "POST", "/ro/4/ai-review/2/approve"), role)  # route re-checks role
        self.assertFalse(allowed("tech", "POST", "/ro/4/ai-review/2/delete"))
        self.assertTrue(allowed(None, "POST", "/internal/local-node/claim"))  # own key, see API tests


class LocalNodeApiTests(unittest.TestCase):
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
        self.env = patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": NODE_KEY})
        self.env.start()
        self.internal = patch.object(internal_messaging, "INTERNAL_API_KEY", "i" * 48)
        self.internal.start()
        with self.Session() as db:
            for uid, name, role in ((1, "owner", "owner"), (2, "crew", "tech"), (3, "desk", "office"), (4, "boss", "admin")):
                db.add(User(id=uid, username=name, full_name=name.title(), password_hash=hash_password(PASSWORD), role=role))
            db.add(Customer(id=1, first_name="Synthetic", last_name="Tester", phone="6025550100", email="t@example.com"))
            db.add(Vehicle(id=1, customer_id=1, year=2014, make="Ford", model="F-150", engine="3.5L", vin="1FTFW1ET5EFA00000"))
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="diagnosing", mileage_in=128400,
                               concern="Check engine light, shakes at idle. Call me at 602-555-0100", tech_notes=""))
            db.add(RepairOrder(id=2, customer_id=1, vehicle_id=1, status="diagnosing", concern="Noise", tech_notes="Original note"))
            db.commit()
        self.worker = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.env.stop()
        self.internal.stop()
        self.engine.dispose()

    # -- helpers ---------------------------------------------------------
    def staff(self, username):
        client = TestClient(app, follow_redirects=False)
        self.assertEqual(client.post("/login", data={"username": username, "password": PASSWORD}).status_code, 302)
        return client

    def request_review(self, client=None, ro=1, codes="P0301", readings=""):
        client = client or self.staff("crew")
        return client.post(f"/ro/{ro}/ai-review", data={"codes": codes, "readings": readings})

    def claim(self, worker="shop-node-1"):
        response = self.worker.post("/internal/local-node/claim", headers=NODE, json={"workerId": worker})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    def result(self, task, output, worker="shop-node-1"):
        return self.worker.post(f"/internal/local-node/tasks/{task['taskId']}/result", headers=NODE,
                                json={"workerId": worker, "leaseToken": task["leaseToken"], "model": "imperial-diagnostics:latest",
                                      "output": output if isinstance(output, str) else json.dumps(output), "durationMs": 1200})

    def review(self, review_id=1):
        with self.Session() as db:
            row = db.get(DiagnosticReview, review_id)
            db.expunge(row)
            return row

    def actions(self, review_id=1):
        with self.Session() as db:
            return [e.action for e in db.query(DiagnosticReviewEvent).filter_by(review_id=review_id).order_by(DiagnosticReviewEvent.id)]

    def age(self, review_id, **fields):
        with self.Session() as db:
            row = db.get(DiagnosticReview, review_id)
            for key, value in fields.items():
                setattr(row, key, value)
            db.commit()

    # -- authentication ----------------------------------------------------
    def test_worker_key_required_and_separate_from_internal_key(self):
        body = {"workerId": "shop-node-1"}
        self.assertEqual(self.worker.post("/internal/local-node/claim", json=body).status_code, 401)
        self.assertEqual(self.worker.post("/internal/local-node/claim", json=body,
                                          headers={"X-Local-Node-Key": "wrong" * 10}).status_code, 401)
        self.assertEqual(self.worker.post("/internal/local-node/claim", json=body,
                                          headers={"X-Local-Node-Key": "i" * 48, "X-Internal-Key": "i" * 48}).status_code, 401)
        # The node key opens nothing else.
        self.assertEqual(self.worker.get("/internal/imperial/tasks/open", headers={"X-Internal-Key": NODE_KEY}).status_code, 401)
        self.assertIn(self.worker.get("/ro/1", headers=NODE).status_code, (302, 303, 401))
        self.assertIn(self.worker.post("/ro/1/ai-review/1/approve", headers=NODE, data={"approved_text": "x"}).status_code, (401, 404))

    def test_feature_off_or_short_key_refuses(self):
        with patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "false"}):
            self.assertEqual(self.worker.post("/internal/local-node/claim", headers=NODE, json={"workerId": "shop-node-1"}).status_code, 503)
            self.assertEqual(self.request_review().status_code, 503)
        with patch.dict(os.environ, {"LOCAL_NODE_WORKER_KEY": "short"}):
            self.assertEqual(self.worker.post("/internal/local-node/claim", headers={"X-Local-Node-Key": "short"},
                                              json={"workerId": "shop-node-1"}).status_code, 503)

    def test_read_only_mode_blocks_worker_writes(self):
        app.state.read_only = True
        self.assertEqual(self.worker.post("/internal/local-node/claim", headers=NODE, json={"workerId": "shop-node-1"}).status_code, 503)

    # -- normal path ---------------------------------------------------------
    def test_normal_request_draft_approve_saves_to_job_with_audit(self):
        crew = self.staff("crew")
        self.assertEqual(self.request_review(crew).status_code, 303)
        review = self.review()
        snapshot = json.loads(review.source_snapshot)
        self.assertEqual(review.status, "queued")
        self.assertNotIn("Synthetic", review.source_snapshot)
        self.assertNotIn("555-0100", review.source_snapshot)
        self.assertNotIn("1FTFW1ET5EFA00000", review.source_snapshot)
        self.assertEqual(snapshot["reported_codes"], [{"code": "P0301", "status": "not supplied"}])

        task = self.claim()
        self.assertEqual(task["model"], "imperial-diagnostics:latest")
        self.assertIn("status_as_supplied", json.dumps(task["format"]))
        self.assertNotIn("Synthetic", json.dumps(task))
        self.assertEqual(self.review().status, "processing")
        self.assertEqual(crew.get("/ro/1/ai-review/1/status").status_code, 200)

        self.assertTrue(self.result(task, good_output()).json()["accepted"])
        self.assertEqual(self.review().status, "draft_ready")
        page = crew.get("/ro/1/ai-review/1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Approve findings", page.text)
        self.assertIn("status: not supplied", page.text)

        edited = guard.draft_to_text(json.loads(self.review().draft_json)) + "\nTech: verified coil swap pending."
        self.assertEqual(crew.post("/ro/1/ai-review/1/approve", data={"approved_text": edited}).status_code, 303)
        approved = self.review()
        self.assertEqual((approved.status, approved.reviewed_by_user_id), ("approved", 2))
        with self.Session() as db:
            notes = db.get(RepairOrder, 1).tech_notes
        self.assertIn("Diagnostic review #1 · technician-approved by Crew", notes)
        self.assertIn("verified coil swap pending", notes)
        self.assertEqual(self.actions(), ["requested", "claimed", "draft_ready", "approved"])
        # Final: cannot decide twice.
        self.assertEqual(crew.post("/ro/1/ai-review/1/reject", data={}).status_code, 409)

    def test_job_page_shows_panel_and_existing_notes_kept(self):
        crew = self.staff("crew")
        page = crew.get("/ro/2")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Request AI diagnostic review", page.text)
        self.request_review(crew, ro=2, codes="")
        page = crew.get("/ro/2")
        self.assertIn("Waiting for the shop workstation", page.text)
        self.assertIn("never checked in", page.text)
        self.assertNotIn("Request AI diagnostic review", page.text)

    def test_reject_leaves_job_untouched(self):
        crew = self.staff("crew")
        self.request_review(crew, ro=2, codes="")
        body = self.result(self.claim(), good_output(reported_codes=[], customer_reported_symptoms=["Noise"], suspected_causes=[],
                                                     missing_information=["When the noise happens"])).json()
        self.assertTrue(body["accepted"], body)
        self.assertEqual(crew.post("/ro/2/ai-review/1/reject", data={"note": "Not useful"}).status_code, 303)
        with self.Session() as db:
            self.assertEqual(db.get(RepairOrder, 2).tech_notes, "Original note")
        self.assertEqual(self.review().status, "rejected")

    # -- authorization -------------------------------------------------------
    def test_only_technician_admin_owner_can_decide(self):
        self.request_review()
        self.result(self.claim(), good_output())
        desk = self.staff("desk")
        self.assertEqual(desk.get("/ro/1/ai-review/1").status_code, 200)
        self.assertEqual(desk.post("/ro/1/ai-review/1/approve", data={"approved_text": "ok"}).status_code, 403)
        self.assertEqual(desk.post("/ro/1/ai-review/1/reject", data={}).status_code, 403)
        anonymous = TestClient(app, follow_redirects=False)
        self.assertEqual(anonymous.post("/ro/1/ai-review/1/approve", data={"approved_text": "ok"}).status_code, 401)
        self.assertEqual(self.review().status, "draft_ready")
        boss = self.staff("boss")
        self.assertEqual(boss.post("/ro/1/ai-review/1/approve", data={"approved_text": ""}).status_code, 422)
        self.assertEqual(boss.post("/ro/1/ai-review/1/approve", data={"approved_text": "Approved text"}).status_code, 303)

    def test_cannot_approve_before_draft_or_across_jobs(self):
        crew = self.staff("crew")
        self.request_review(crew)
        self.assertEqual(crew.post("/ro/1/ai-review/1/approve", data={"approved_text": "x"}).status_code, 409)
        self.assertEqual(crew.post("/ro/2/ai-review/1/approve", data={"approved_text": "x"}).status_code, 404)

    def test_database_refuses_approval_without_reviewer_or_text(self):
        self.request_review()
        with self.Session() as db:
            row = db.get(DiagnosticReview, 1)
            row.status = "approved"
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()
            row = db.get(DiagnosticReview, 1)
            row.status, row.reviewed_by_user_id, row.reviewed_at = "approved", 2, datetime.now(timezone.utc)
            with self.assertRaises(IntegrityError):
                db.commit()  # still no approved_text

    # -- duplicates, offline, restart -----------------------------------------
    def test_duplicate_requests_and_claims(self):
        crew = self.staff("crew")
        self.request_review(crew)
        self.request_review(crew)
        with self.Session() as db:
            self.assertEqual(db.query(DiagnosticReview).count(), 1)
            db.add(DiagnosticReview(repair_order_id=1, requested_by_user_id=2, status="queued",
                                    source_snapshot="{}", prompt_version="x"))
            with self.assertRaises(IntegrityError):
                db.commit()  # partial unique index: one active review per job
        first = self.claim("shop-node-1")
        self.assertIsNotNone(first)
        self.assertIsNone(self.claim("shop-node-2"))

    def test_worker_restart_reclaims_and_stale_worker_cannot_overwrite(self):
        self.request_review()
        old = self.claim("shop-node-1")
        self.age(1, lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
        new = self.claim("shop-node-1b")
        self.assertEqual(new["taskId"], old["taskId"])
        self.assertEqual(new["attempt"], 2)
        self.assertEqual(self.result(old, good_output(), worker="shop-node-1").status_code, 409)
        self.assertTrue(self.result(new, good_output(), worker="shop-node-1b").json()["accepted"])
        self.assertIn("reclaimed", self.actions())

    def test_lease_expiring_on_last_attempt_fails_cleanly(self):
        self.request_review()
        self.claim()
        self.age(1, attempt_count=3, lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.assertIsNone(self.claim("shop-node-2"))
        review = self.review()
        self.assertEqual(review.status, "failed")
        self.assertIn("stopped responding", review.last_error)

    def test_node_offline_detection(self):
        with self.Session() as db:
            self.assertFalse(local_node.node_status(db)["online"])
        self.assertEqual(self.worker.post("/internal/local-node/heartbeat", headers=NODE,
                                          json={"workerId": "shop-node-1", "version": "1.0.0", "model": "imperial-diagnostics:latest",
                                                "ollamaOk": True}).status_code, 200)
        with self.Session() as db:
            self.assertTrue(local_node.node_status(db)["online"])
            row = db.get(LocalNodeHeartbeat, "shop-node-1")
            row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            db.commit()
            self.assertEqual(local_node.node_status(db)["label"], "Workstation offline")
        self.worker.post("/internal/local-node/heartbeat", headers=NODE, json={"workerId": "shop-node-1", "ollamaOk": False})
        with self.Session() as db:
            self.assertIn("Ollama is not responding", local_node.node_status(db)["label"])

    # -- invalid output, errors, recovery ------------------------------------
    def test_invalid_output_retries_then_fails_then_manual_retry(self):
        crew = self.staff("crew")
        self.request_review(crew)
        for attempt in (1, 2, 3):
            task = self.claim()
            self.assertIsNotNone(task, attempt)
            body = self.result(task, "Replace coil 1 now, P0301 is active. $120.").json()
            self.assertFalse(body["accepted"])
            self.assertEqual(body["violations"], ["not_json"])
            if attempt < 3:
                self.assertEqual(self.review().status, "retry_wait")
                self.assertIsNone(self.claim())  # waits for retry_after
                self.age(1, retry_after=datetime.now(timezone.utc) - timedelta(seconds=1))
        review = self.review()
        self.assertEqual(review.status, "failed")
        self.assertIn("not_json", review.last_error)
        self.assertIsNone(review.draft_json)
        self.assertEqual(crew.post("/ro/1/ai-review/1/retry").status_code, 303)
        self.assertEqual(self.review().status, "queued")
        self.assertIsNotNone(self.claim())

    def test_unsafe_but_valid_json_never_becomes_a_draft(self):
        self.request_review()
        unsafe = good_output(reported_codes=[{"code": "P0301", "status_as_supplied": "active", "meaning": "misfire"}],
                             proposed_repairs=[{"repair": "Replace coil 1", "justified_by_confirmed_findings": ["P0301"]}])
        body = self.result(self.claim(), unsafe).json()
        self.assertIn("code_status_changed:P0301:active", body["violations"])
        self.assertIn("repair_justified_by_code_only", body["violations"])
        self.assertIsNone(self.review().draft_json)

    def test_worker_error_reporting(self):
        self.request_review()
        task = self.claim()
        response = self.worker.post(f"/internal/local-node/tasks/{task['taskId']}/error", headers=NODE,
                                    json={"workerId": "shop-node-1", "leaseToken": task["leaseToken"],
                                          "error": "Ollama timed out", "retryable": True})
        self.assertEqual(response.json()["status"], "retry_wait")
        self.age(1, retry_after=datetime.now(timezone.utc) - timedelta(seconds=1))
        task = self.claim()
        response = self.worker.post(f"/internal/local-node/tasks/{task['taskId']}/error", headers=NODE,
                                    json={"workerId": "shop-node-1", "leaseToken": task["leaseToken"],
                                          "error": "Model not installed", "retryable": False})
        self.assertEqual(response.json()["status"], "failed")
        self.assertIn("worker_error", self.actions())

    def test_cancel_stops_worker_result(self):
        crew = self.staff("crew")
        self.request_review(crew)
        task = self.claim()
        self.assertEqual(crew.post("/ro/1/ai-review/1/cancel").status_code, 303)
        self.assertEqual(self.result(task, good_output()).status_code, 409)
        self.assertEqual(self.review().status, "cancelled")

    def test_eval_endpoints_store_nothing(self):
        cases = self.worker.get("/internal/local-node/eval-cases", headers=NODE).json()["cases"]
        self.assertEqual([c["name"] for c in cases][0], "p0301_status_not_supplied")
        verdict = self.worker.post("/internal/local-node/validate", headers=NODE,
                                   json={"case": "p0301_status_not_supplied", "output": json.dumps(good_output())}).json()
        self.assertTrue(verdict["ok"], verdict)
        with self.Session() as db:
            self.assertEqual(db.query(DiagnosticReview).count(), 0)


class PreviewGateTests(unittest.TestCase):
    """QA previews hide everything behind an access code; only the key-checked
    node API may pass, so the workstation (which cannot fill in a browser form)
    still has to present LOCAL_NODE_WORKER_KEY."""

    ENV = {"PUBLIC_PREVIEW_MODE": "true", "PREVIEW_ACCESS_CODE": "made-up-code", "RAILWAY_ENVIRONMENT_NAME": "preview",
           "RAILWAY_PROJECT_ID": "other", "LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": NODE_KEY}

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)

        def override():
            with Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        self.client = TestClient(app, follow_redirects=False)

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def test_node_api_reaches_key_check_but_nothing_else_skips_the_gate(self):
        with patch.dict(os.environ, self.ENV):
            body = {"workerId": "shop-node-1"}
            self.assertEqual(self.client.post("/internal/local-node/claim", json=body).status_code, 401)
            self.assertEqual(self.client.post("/internal/local-node/claim", json=body, headers=NODE).status_code, 200)
            self.assertEqual(self.client.get("/internal/local-node/eval-cases").status_code, 401)
            for path in ("/ro/1", "/ro/1/ai-review/1", "/internal/imperial/tasks/open", "/internal/local-node",
                         "/internal/local-nodeX/claim", "/dashboard"):
                response = self.client.get(path)
                self.assertEqual((response.status_code, response.headers.get("location")), (303, "/preview-access"), path)


class FeatureOffTests(unittest.TestCase):
    """Production stays as-is until the flag and migration are approved."""

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            for table in ("diagnostic_review_events", "diagnostic_reviews", "local_node_heartbeats"):
                conn.execute(text(f"DROP TABLE {table}"))
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        self.read_only = app.state.read_only
        app.state.read_only = False
        with self.Session() as db:
            db.add(User(id=2, username="crew", password_hash=hash_password(PASSWORD), role="tech"))
            db.add(Customer(id=1, first_name="Synthetic", last_name="Tester"))
            db.add(Vehicle(id=1, customer_id=1, year=2014, make="Ford", model="F-150"))
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="diagnosing", concern="Noise"))
            db.commit()
        self.client = TestClient(app, follow_redirects=False)
        self.client.post("/login", data={"username": "crew", "password": PASSWORD})

    def tearDown(self):
        app.state.read_only = self.read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def test_job_page_unchanged_when_disabled_and_unmigrated(self):
        with patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "false"}):
            page = self.client.get("/ro/1")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Shop AI diagnostic review", page.text)

    def test_enabled_without_migration_shows_message_not_error(self):
        with patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": NODE_KEY}):
            page = self.client.get("/ro/1")
        self.assertEqual(page.status_code, 200)
        self.assertIn("migration was applied", page.text)

    def test_readiness_ignores_unapplied_tables_when_disabled(self):
        from app import recovery
        with patch.object(recovery, "engine", self.engine), patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "false"}):
            audit = recovery.database_audit(include_counts=False)
        self.assertNotIn("diagnostic_reviews", audit["missing_tables"])
        with patch.object(recovery, "engine", self.engine), patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true"}):
            audit = recovery.database_audit(include_counts=False)
        self.assertIn("diagnostic_reviews", audit["missing_tables"])


if __name__ == "__main__":
    unittest.main()
