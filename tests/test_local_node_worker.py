"""End-to-end: the real worker script against the real app over HTTP and a fake Ollama.

Everything runs on 127.0.0.1 with synthetic data. No real model, Railway or Quo.
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import fixture_environment  # noqa: F401
import uvicorn
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.local_node_model import DiagnosticReview, LocalNodeHeartbeat
from app.main import app
from app.models import Customer, RepairOrder, User, Vehicle

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local_node"))
import worker  # noqa: E402

NODE_KEY = "w" * 48
SAFE_DRAFT = {"customer_reported_symptoms": ["Engine shakes at idle"], "reported_codes": [],
              "technician_confirmed_findings": [], "suspected_causes": [],
              "recommended_tests": [{"test": "Check misfire counters per cylinder", "purpose": "Find the cylinder"}],
              "proposed_repairs": [], "missing_information": ["Code status and freeze frame data"], "cautions": []}
UNSAFE = {**SAFE_DRAFT, "reported_codes": [{"code": "P0301", "status_as_supplied": "active", "meaning": "misfire"}],
          "proposed_repairs": [{"repair": "Replace coil 1", "justified_by_confirmed_findings": ["P0301"]}]}
SWAP_BASE = {**SAFE_DRAFT, "reported_codes": [{"code": "P0301", "status_as_supplied": "active", "meaning": "misfire"}]}
# Synthetic eval cases 3/4: a correct reading, and the reading the shop model gave in QA (2026-11-11).
SWAP_ANSWERS = {
    "EVAL-3": {"right": [("Coil originally in cylinder 1, now in cylinder 3", "high")],
               "observed": [("Faulty ignition coil 3", "high"), ("Cylinder 3 wiring", "medium")]},
    "EVAL-4": {"right": [("Spark plug fault on cylinder 1", "high"), ("Ignition coil, ruled out by swap", "low")],
               "observed": [("Cylinder 1 ignition coil faulty", "high"), ("Cylinder 1 wiring", "medium")]},
}


def swap_answer(job, reading):
    causes = [{"cause": c, "basis": "coil swap result", "likelihood": lk} for c, lk in SWAP_ANSWERS[job][reading]]
    return {**SWAP_BASE, "suspected_causes": causes, "recommended_tests": [
        {"test": "Inspect the cylinder 1 spark plug and injector wiring", "purpose": "Check the cylinder that misfires"}]}


class FakeOllama(BaseHTTPRequestHandler):
    mode = "safe"
    calls = []

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/api/version":
            return self.reply(200, {"version": "0.12.0-fake"})
        if self.path == "/api/tags":
            return self.reply(200, {"models": [{"name": "imperial-diagnostics:latest"}, {"name": "qwen3:8b-q4_K_M"}]})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeOllama.calls.append(body)
        if FakeOllama.mode == "error":
            return self.reply(500, {"error": "CUDA out of memory"})
        if body["format"] == "json":
            content = '{"ok": true}'
        else:
            prompt = body["messages"][-1]["content"]
            job = next((j for j in SWAP_ANSWERS if f'"{j}"' in prompt), None)
            if job and FakeOllama.mode in ("safe", "observed_swap"):
                draft = swap_answer(job, "observed" if FakeOllama.mode == "observed_swap" else "right")
            else:
                draft = UNSAFE if FakeOllama.mode == "unsafe" else SAFE_DRAFT
            content = json.dumps(draft)
        self.reply(200, {"message": {"role": "assistant", "content": content}, "done": True})


class WorkerEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine)

        def override():
            with cls.Session() as db:
                yield db
        app.dependency_overrides[get_db] = override
        cls.read_only = app.state.read_only
        app.state.read_only = False
        cls.env = patch.dict(os.environ, {"LOCAL_NODE_ENABLED": "true", "LOCAL_NODE_WORKER_KEY": NODE_KEY})
        cls.env.start()

        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.app_port = cls.server.servers[0].sockets[0].getsockname()[1]
        cls.ollama = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        threading.Thread(target=cls.ollama.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.ollama.shutdown()
        cls.env.stop()
        app.state.read_only = cls.read_only
        app.dependency_overrides.pop(get_db, None)
        cls.tmp.cleanup()

    def setUp(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        FakeOllama.mode, FakeOllama.calls = "safe", []
        with self.Session() as db:
            db.add(User(id=2, username="crew", password_hash=hash_password("fixture-password-1"), role="tech"))
            db.add(Customer(id=1, first_name="Synthetic", last_name="Tester"))
            db.add(Vehicle(id=1, customer_id=1, year=2014, make="Ford", model="F-150"))
            db.add(RepairOrder(id=1, customer_id=1, vehicle_id=1, status="diagnosing", concern="Shakes at idle"))
            db.add(DiagnosticReview(repair_order_id=1, requested_by_user_id=2, status="queued", prompt_version="diag-v1",
                                    source_snapshot=json.dumps({"job_reference": "RO-1", "vehicle": {}, "technician_notes": "",
                                                                "customer_reported_concern": "Shakes at idle",
                                                                "technician_readings": "",
                                                                "reported_codes": [{"code": "P0301", "status": "not supplied"}]})))
            db.commit()
        self.config = self.write_config()

    def write_config(self, **changes):
        values = {"backendUrl": f"http://127.0.0.1:{self.app_port}", "workerId": "shop-test-node",
                  "ollamaUrl": f"http://127.0.0.1:{self.ollama.server_address[1]}", "pollSeconds": 1,
                  "logDir": os.path.join(self.tmp.name, "logs"), **changes}
        path = os.path.join(self.tmp.name, "config.json")
        Path(path).write_text("﻿" + json.dumps(values), encoding="utf-8")  # BOM like PowerShell 5.1
        return path

    def run_worker(self, *args, key=NODE_KEY):
        # The app under test runs in this same process and reads its expected key
        # from os.environ, so hand the worker its key without touching the server's.
        out = io.StringIO()
        if len(key) < 32:
            presented = patch.object(worker, "worker_key", side_effect=worker.ConfigError("too short"))
        else:
            presented = patch.object(worker, "worker_key", return_value=key)
        with presented, redirect_stdout(out):
            code = worker.main(["--config", self.config, *args])
        return code, out.getvalue()

    def status(self):
        with self.Session() as db:
            row = db.get(DiagnosticReview, 1)
            return row.status, row.last_error, row.draft_json

    def test_worker_key_comes_from_environment(self):
        with patch.dict(os.environ, {"LOCAL_NODE_WORKER_KEY": ""}):
            with self.assertRaises(worker.ConfigError):
                worker.worker_key()
        with patch.dict(os.environ, {"LOCAL_NODE_WORKER_KEY": NODE_KEY}):
            self.assertEqual(worker.worker_key(), NODE_KEY)

    def test_key_file_is_decrypted_by_the_worker(self):
        key_file = os.path.join(self.tmp.name, "worker.key")
        Path(key_file).write_text("0a0b0c0d\r\n", encoding="ascii")  # hex DPAPI blob as PowerShell writes it
        with patch.object(worker, "_dpapi_unprotect", return_value=NODE_KEY.encode("utf-16-le")) as unprotect:
            self.assertEqual(worker.worker_key(key_file), NODE_KEY)
        unprotect.assert_called_once_with(bytes.fromhex("0a0b0c0d"))
        Path(key_file).write_text("not hex", encoding="ascii")
        with self.assertRaises(worker.ConfigError):
            worker.worker_key(key_file)
        if os.name != "nt":
            with self.assertRaises(worker.ConfigError):
                worker._dpapi_unprotect(b"\x01\x02")

    def test_background_start_with_key_file_processes_a_review(self):
        key_file = os.path.join(self.tmp.name, "worker.key")
        Path(key_file).write_text("00", encoding="ascii")
        out = io.StringIO()
        with patch.object(worker, "_dpapi_unprotect", return_value=NODE_KEY.encode("utf-16-le")), redirect_stdout(out):
            self.assertEqual(worker.main(["--config", self.config, "--key-file", key_file, "--once"]), 0)
        self.assertEqual(self.status()[0], "draft_ready")

    def test_startup_errors_are_written_to_a_file_for_pythonw(self):
        bad = os.path.join(self.tmp.name, "cfg", "config.json")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        Path(bad).write_text(json.dumps({"backendUrl": "http://example.com"}), encoding="utf-8")
        with patch.object(worker, "worker_key", return_value=NODE_KEY), redirect_stdout(io.StringIO()):
            self.assertEqual(worker.main(["--config", bad, "--once"]), 2)
        log = Path(self.tmp.name, "logs", "worker-startup-errors.log").read_text(encoding="utf-8")
        self.assertIn("backendUrl must be https", log)

    def test_once_processes_queued_review_into_draft(self):
        code, _ = self.run_worker("--once")
        self.assertEqual(code, 0)
        status, error, draft = self.status()
        self.assertEqual(status, "draft_ready", error)
        self.assertIsNotNone(draft)
        sent = FakeOllama.calls[-1]
        self.assertEqual(sent["model"], "imperial-diagnostics:latest")
        self.assertFalse(sent["stream"])
        self.assertIn("status_as_supplied", json.dumps(sent["format"]))
        self.assertIn('"status": "not supplied"', sent["messages"][1]["content"])
        with self.Session() as db:
            beat = db.get(LocalNodeHeartbeat, "shop-test-node")
            self.assertEqual((beat.ollama_ok, beat.version), ("yes", worker.VERSION))

    def test_unsafe_model_answer_is_rejected_by_railway(self):
        FakeOllama.mode = "unsafe"
        self.assertEqual(self.run_worker("--once")[0], 0)
        status, error, draft = self.status()
        self.assertEqual(status, "retry_wait")
        self.assertIn("code_status_changed:P0301:active", error)
        self.assertIsNone(draft)

    def test_ollama_error_is_reported_for_retry(self):
        FakeOllama.mode = "error"
        self.assertEqual(self.run_worker("--once")[0], 0)
        status, error, _ = self.status()
        self.assertEqual(status, "retry_wait")
        self.assertIn("HTTP 500", error)

    def test_ollama_down_leaves_task_queued_and_reports_offline(self):
        self.config = self.write_config(ollamaUrl="http://127.0.0.1:9")  # nothing listens on discard port
        self.assertEqual(self.run_worker("--once")[0], 0)
        self.assertEqual(self.status()[0], "queued")
        with self.Session() as db:
            self.assertEqual(db.get(LocalNodeHeartbeat, "shop-test-node").ollama_ok, "no")

    def test_wrong_key_is_refused(self):
        self.assertEqual(self.run_worker("--once", key="x" * 48)[0], 1)
        self.assertEqual(self.status()[0], "queued")

    def test_check_and_eval_modes(self):
        code, output = self.run_worker("--check")
        self.assertEqual(code, 0, output)
        self.assertIn("[PASS] Railway reachable and worker key accepted", output)
        self.assertIn("[PASS] Model 'imperial-diagnostics:latest' installed", output)
        code, output = self.run_worker("--eval")
        self.assertEqual(code, 0, output)
        self.assertIn("[PASS] p0301_status_not_supplied", output)
        self.assertIn("[PASS] confirmed_finding_supports_repair", output)
        self.assertIn("[PASS] swap_fault_stayed", output)
        self.assertEqual(self.status()[0], "queued")  # eval never touches real reviews

    def test_eval_fails_the_swap_readings_seen_in_qa(self):
        FakeOllama.mode = "observed_swap"
        code, output = self.run_worker("--eval")
        self.assertEqual(code, 1, output)
        self.assertIn("[PASS] p0301_status_not_supplied", output)
        self.assertIn("[FAIL] confirmed_finding_supports_repair", output)
        self.assertIn("violation: swap_wrong_coil_blamed", output)
        self.assertIn("[FAIL] swap_fault_stayed", output)
        self.assertIn("violation: swap_ruled_out_coil_ranked", output)
        self.assertIn("2 case(s) failed", output)
        self.assertEqual(self.status()[0], "queued")

    def test_config_refuses_exposed_ollama_and_plain_http_backend(self):
        self.config = self.write_config(ollamaUrl="http://192.168.1.20:11434")
        self.assertEqual(self.run_worker("--once")[0], 2)
        self.config = self.write_config(backendUrl="http://imperial-qa.example.com")
        self.assertEqual(self.run_worker("--once")[0], 2)
        self.assertEqual(self.run_worker("--once", key="short")[0], 2)

    def test_second_worker_on_same_machine_refused(self):
        lock_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(lock_dir, exist_ok=True)
        holder = worker.SingleInstance(os.path.join(lock_dir, "worker.lock"))
        try:
            self.assertEqual(self.run_worker("--once")[0], 3)
        finally:
            holder.handle.close()


if __name__ == "__main__":
    unittest.main()
