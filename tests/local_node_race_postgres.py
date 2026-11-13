"""Concurrent Local Node lease races on a disposable PostgreSQL database.

A stale worker must never overwrite a review after another worker reclaimed it,
after the final attempt expired, or after staff cancelled it. The race window
(between the lease pre-check and the write) is held open deterministically by
slowing the stale worker's validation step, then a second actor acts inside it.
Runs only against the CI fixture database on loopback, with synthetic data.
"""
import json
import os
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_environment  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import diagnostic_guard as guard  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.local_node_model import DiagnosticReview, DiagnosticReviewEvent  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, RepairOrder, User, Vehicle  # noqa: E402
from app.routes import local_node  # noqa: E402

URL = make_url(os.environ["TEST_DATABASE_URL"])
assert URL.host in ("localhost", "127.0.0.1")
assert URL.database == "appointment_test"
ROOT = Path(__file__).resolve().parents[1]
KEY = "r" * 48
NODE = {"X-Local-Node-Key": KEY}
PASSWORD = "fixture-password-1"
SNAPSHOT = {"job_reference": "RO-race", "vehicle": {}, "customer_reported_concern": "Shakes at idle",
            "technician_notes": "", "technician_readings": "",
            "reported_codes": [{"code": "P0301", "status": "not supplied"}]}
GOOD = {"customer_reported_symptoms": ["Shakes at idle"],
        "reported_codes": [{"code": "P0301", "status_as_supplied": "not supplied", "meaning": "Cylinder 1 misfire"}],
        "technician_confirmed_findings": [], "suspected_causes": [], "proposed_repairs": [],
        "recommended_tests": [{"test": "Check misfire counters", "purpose": "Find the cylinder"}],
        "missing_information": ["Code status"], "cautions": []}

engine = create_engine(URL, pool_size=10)
Session = sessionmaker(bind=engine)


def slowed(real, delay=0.4):
    def wrapper(*args, **kwargs):
        time.sleep(delay)  # hold the stale worker between its lease check and its write
        return real(*args, **kwargs)
    return wrapper


class LeaseRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text((ROOT / "migrations/20261111_local_ai_node.down.sql").read_text()))
        Base.metadata.create_all(engine, tables=[t for name, t in Base.metadata.tables.items()
                                                  if name not in ("diagnostic_reviews", "diagnostic_review_events",
                                                                  "local_node_heartbeats")])
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text((ROOT / "migrations/20261111_local_ai_node.sql").read_text()))  # with triggers

        def override():
            with Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        cls.read_only = app.state.read_only
        app.state.read_only = False
        cls.env = patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": KEY})
        cls.env.start()
        with Session() as db:
            tech = User(username=f"race-tech-{time.time_ns()}", password_hash=hash_password(PASSWORD), role="tech")
            customer = Customer(first_name="Synthetic", last_name="Race")
            db.add_all([tech, customer])
            db.flush()
            vehicle = Vehicle(customer_id=customer.id, year=2014, make="Ford", model="F-150")
            db.add(vehicle)
            db.flush()
            cls.tech, cls.customer, cls.vehicle = tech.username, customer.id, vehicle.id
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        app.state.read_only = cls.read_only
        app.dependency_overrides.pop(get_db, None)

    # -- helpers ---------------------------------------------------------------
    def new_review(self, **fields):
        """A fresh job with one queued review, so no other row is claimable."""
        with Session() as db:
            db.query(DiagnosticReview).filter(DiagnosticReview.status.in_(("queued", "retry_wait"))).update(
                {"status": "cancelled"}, synchronize_session=False)
            ro = RepairOrder(customer_id=self.customer, vehicle_id=self.vehicle, status="diagnosing", concern="race")
            db.add(ro)
            db.flush()
            user_id = db.query(User.id).filter(User.username == self.tech).scalar()
            review = DiagnosticReview(repair_order_id=ro.id, vehicle_id=self.vehicle, requested_by_user_id=user_id,
                                      status="queued", source_snapshot=json.dumps(SNAPSHOT), prompt_version="race")
            db.add(review)
            db.commit()
            if fields:
                db.query(DiagnosticReview).filter_by(id=review.id).update(fields, synchronize_session=False)
                db.commit()
            return ro.id, review.id

    def claim(self, worker):
        response = TestClient(app).post("/internal/local-node/claim", headers=NODE, json={"workerId": worker})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    def expire(self, review_id, **extra):
        with Session() as db:
            db.query(DiagnosticReview).filter_by(id=review_id).update(
                {"lease_until": datetime.now(timezone.utc) - timedelta(seconds=1), **extra}, synchronize_session=False)
            db.commit()

    def post_result(self, task, worker, out):
        response = TestClient(app).post(f"/internal/local-node/tasks/{task['taskId']}/result", headers=NODE, json={
            "workerId": worker, "leaseToken": task["leaseToken"], "output": json.dumps(GOOD)})
        out["result"] = response

    def post_error(self, task, worker, out):
        response = TestClient(app).post(f"/internal/local-node/tasks/{task['taskId']}/error", headers=NODE, json={
            "workerId": worker, "leaseToken": task["leaseToken"], "error": "Ollama timed out", "retryable": True})
        out["result"] = response

    def state(self, review_id):
        with Session() as db:
            row = db.get(DiagnosticReview, review_id)
            events = [(e.actor, e.action) for e in db.query(DiagnosticReviewEvent)
                      .filter_by(review_id=review_id).order_by(DiagnosticReviewEvent.id)]
            return row.status, row.lease_owner, row.lease_token, row.draft_json, events

    def stale_call_during(self, target, act):
        """Start the stale worker's call, act inside its window, then wait for it."""
        out = {}
        thread = threading.Thread(target=target, args=(out,))
        thread.start()
        time.sleep(0.15)
        act_result = act()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        return out["result"], act_result

    # -- deterministic races -----------------------------------------------------
    def test_stale_result_loses_to_reclaim(self):
        _, review_id = self.new_review()
        old = self.claim("node-a")
        self.expire(review_id)
        with patch.object(local_node.guard, "validate_output", slowed(guard.validate_output)):
            stale, new = self.stale_call_during(lambda out: self.post_result(old, "node-a", out),
                                                lambda: self.claim("node-b"))
        self.assertEqual(new["taskId"], review_id)
        self.assertEqual(stale.status_code, 409, stale.text)
        status, owner, token, draft, events = self.state(review_id)
        self.assertEqual((status, owner, token, draft), ("processing", "node-b", new["leaseToken"], None))
        self.assertEqual(events, [("node:node-a", "claimed"), ("node:node-b", "reclaimed")])
        # The winner can still finish normally.
        out = {}
        self.post_result(new, "node-b", out)
        self.assertEqual(out["result"].status_code, 200)
        self.assertEqual(self.state(review_id)[4][-1], ("node:node-b", "draft_ready"))

    def test_stale_error_loses_to_reclaim(self):
        _, review_id = self.new_review()
        old = self.claim("node-a")
        self.expire(review_id)
        with patch.object(local_node, "_retry_or_fail", slowed(local_node._retry_or_fail)):
            stale, new = self.stale_call_during(lambda out: self.post_error(old, "node-a", out),
                                                lambda: self.claim("node-b"))
        self.assertEqual(stale.status_code, 409, stale.text)
        status, owner, token, _, events = self.state(review_id)
        self.assertEqual((status, owner, token), ("processing", "node-b", new["leaseToken"]))
        self.assertEqual(events, [("node:node-a", "claimed"), ("node:node-b", "reclaimed")])

    def test_stale_result_loses_to_final_attempt_expiry(self):
        _, review_id = self.new_review()
        old = self.claim("node-a")
        self.expire(review_id, attempt_count=3)
        with patch.object(local_node.guard, "validate_output", slowed(guard.validate_output)):
            stale, new = self.stale_call_during(lambda out: self.post_result(old, "node-a", out),
                                                lambda: self.claim("node-b"))
        self.assertIsNone(new)
        self.assertEqual(stale.status_code, 409, stale.text)
        status, owner, token, draft, events = self.state(review_id)
        self.assertEqual((status, owner, token, draft), ("failed", None, None, None))
        self.assertEqual(events, [("node:node-a", "claimed"), ("system", "lease_expired_failed")])

    def test_stale_result_loses_to_staff_cancel(self):
        ro_id, review_id = self.new_review()
        old = self.claim("node-a")
        staff = TestClient(app, follow_redirects=False)
        self.assertEqual(staff.post("/login", data={"username": self.tech, "password": PASSWORD}).status_code, 302)
        with patch.object(local_node.guard, "validate_output", slowed(guard.validate_output)):
            stale, cancelled = self.stale_call_during(lambda out: self.post_result(old, "node-a", out),
                                                      lambda: staff.post(f"/ro/{ro_id}/ai-review/{review_id}/cancel"))
        self.assertEqual(cancelled.status_code, 303)
        self.assertEqual(stale.status_code, 409, stale.text)
        status, _, _, draft, events = self.state(review_id)
        self.assertEqual((status, draft), ("cancelled", None))
        self.assertEqual([a for _, a in events], ["claimed", "cancelled"])

    # -- free-running concurrency --------------------------------------------------
    def test_free_running_result_vs_reclaim_has_exactly_one_winner(self):
        outcomes = {"result": 0, "reclaim": 0}
        for _ in range(25):
            _, review_id = self.new_review()
            old = self.claim("node-a")
            self.expire(review_id)
            barrier, out = threading.Barrier(2), {}

            def stale():
                barrier.wait()
                self.post_result(old, "node-a", out)

            def fresh():
                barrier.wait()
                out["claim"] = TestClient(app).post("/internal/local-node/claim", headers=NODE,
                                                    json={"workerId": "node-b"}).json()["task"]
            threads = [threading.Thread(target=stale), threading.Thread(target=fresh)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            status, owner, token, draft, events = self.state(review_id)
            actions = [a for _, a in events]
            if out["result"].status_code == 200:
                outcomes["result"] += 1
                self.assertEqual((status, token), ("draft_ready", None))
                self.assertIsNotNone(draft)
                self.assertNotIn("reclaimed", actions)
                self.assertTrue(out["claim"] is None or out["claim"]["taskId"] != review_id)
            else:
                outcomes["reclaim"] += 1
                self.assertEqual(out["result"].status_code, 409)
                self.assertEqual((status, owner, token, draft), ("processing", "node-b", out["claim"]["leaseToken"], None))
                self.assertNotIn("draft_ready", actions)
            self.assertEqual(actions.count("claimed"), 1)
        print(f"free-running rounds: {outcomes}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
