"""Mock-only integration tests: no Groq calls, Github writes or customer SMS."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fixture_environment  # noqa: F401
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.dev_work_model import DevWorkEvent, DevWorkJob
from app.imperial_task_model import ImperialTask, ImperialTaskAudit
from app.main import app
from app.routes import dev_work, internal_messaging
from scripts import free_groq_worker as runner


class FreeWorkerSafetyTests(unittest.TestCase):
    def test_path_allowlist_rejects_sensitive_or_untracked_paths(self):
        self.assertTrue(runner.safe_path("app/templates/site/home.html", "website"))
        self.assertTrue(runner.safe_path("app/static/site.css", "website"))
        self.assertFalse(runner.safe_path("app/routes/dispatch.py", "website"))
        self.assertFalse(runner.safe_path("migrations/20261110_dev_work_runner.sql", "website"))
        self.assertFalse(runner.safe_path("../.env", "website"))
        self.assertFalse(runner.safe_path("/etc/passwd", "website"))
        self.assertFalse(runner.safe_path("docs/REQUIREMENTS-REGISTER.md", "website"))
        self.assertFalse(runner.safe_path("app/templates/site/home.html", "finance"))
        self.assertFalse(runner.safe_path("C:\\secrets.txt", "website"))

    def test_private_customer_data_is_blocked_before_cloud_inference(self):
        allowed = "Continue working on the website navigation"
        self.assertTrue(runner.safe_request(allowed))
        for instruction in (
            "Fix website and contact person@example.com",
            "Continue the site for customer +1 (602) 555-0123",
            "Continue work using vehicle VIN 1FADP3F20GL123456",
            "Improve quote; my cost was $81.83",
            "Continue the site and retrieve my API key",
            "Continue work using bank account details",
        ):
            self.assertFalse(runner.safe_request(instruction), instruction)

    def test_model_edits_must_be_static_and_not_add_external_requests(self):
        for text in (
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(1)>",
            "javascript:alert(1)",
            "fetch('/api/secret')",
            "<iframe src='x'>",
            "{% for customer in customers %}",
            "{{ config.SECRET_KEY }}",
            "https://example.com/track?key=secret",
            "<img src='//tracker.example/pixel'>",
            "@import url(//tracker.example/style.css);",
            "background: url(data:image/svg+xml;base64,PHN2Zz4=)",
            "<meta http-equiv='refresh' content='0;url=/internal'>",
            "<link href='blob:untrusted' rel='stylesheet'>",
        ):
            self.assertFalse(runner.safe_replacement(text), text)
        self.assertTrue(runner.safe_replacement("<p>Book a diagnostic online</p>"))

    def test_sensitive_request_never_reaches_model_planning(self):
        with patch.object(runner, "eligible_files", return_value=["app/templates/site/home.html"]):
            with patch.object(runner, "groq_json", side_effect=AssertionError("Cloud model called")):
                with self.assertRaisesRegex(runner.WorkerFailure, "private data"):
                    runner.plan_and_apply({
                        "projectKey": "website",
                        "instruction": "Continue site updates with my cost $81.83",
                    })

    def test_single_file_mock_run_changes_only_approved_static_markup(self):
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            try:
                os.chdir(root)
                target = Path("app/templates/site/home.html")
                target.parent.mkdir(parents=True)
                target.write_text("<h1>Old heading</h1>", encoding="utf-8")
                answers = [
                    {"path": str(target), "reason": "Relevant homepage"},
                    {"path": str(target), "find": "<h1>Old heading</h1>",
                     "replace": "<h1>Updated heading</h1>", "summary": "Clarify homepage"},
                ]
                with patch.object(runner, "eligible_files", return_value=[str(target)]):
                    with patch.object(runner, "groq_json", side_effect=answers) as groq:
                        path, summary = runner.plan_and_apply({
                            "projectKey": "website",
                            "instruction": "Continue working on the website navigation",
                        })
                self.assertEqual(path, str(target))
                self.assertEqual(summary, "Clarify homepage")
                self.assertEqual(groq.call_count, 2)
                self.assertEqual(target.read_text(encoding="utf-8"), "<h1>Updated heading</h1>")
            finally:
                os.chdir(previous)

    def test_model_generated_active_markup_is_rejected_without_writing(self):
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            try:
                os.chdir(root)
                target = Path("app/templates/site/home.html")
                target.parent.mkdir(parents=True)
                original = "<h1>Old heading</h1>"
                target.write_text(original, encoding="utf-8")
                answers = [
                    {"path": str(target), "reason": "Relevant homepage"},
                    {"path": str(target), "find": original,
                     "replace": "<script>fetch('/internal')</script>", "summary": "unsafe"},
                ]
                with patch.object(runner, "eligible_files", return_value=[str(target)]):
                    with patch.object(runner, "groq_json", side_effect=answers):
                        with self.assertRaisesRegex(runner.WorkerFailure, "unsupported active markup"):
                            runner.plan_and_apply({
                                "projectKey": "website",
                                "instruction": "Continue working on the website navigation",
                            })
                self.assertEqual(target.read_text(encoding="utf-8"), original)
            finally:
                os.chdir(previous)

    def test_private_or_internal_source_file_never_reaches_model(self):
        self.assertFalse(runner.safe_path("docs/IMPERIAL-ASSISTANT-CORE-CONTRACT.md", "website"))
        self.assertFalse(runner.safe_path("docs/IMPERIAL-ASSISTANT-CORE-CONTRACT.md", "operations"))
        self.assertFalse(runner.safe_source_content("customer email: customer@example.com"))
        self.assertFalse(runner.safe_source_content("Please do not expose password in this template"))
        self.assertTrue(runner.safe_source_content("<h1>Welcome to Imperial Auto Care</h1>"))
        with TemporaryDirectory() as root:
            previous = os.getcwd()
            try:
                os.chdir(root)
                target = Path("app/templates/site/home.html")
                target.parent.mkdir(parents=True)
                original = "<p>Customer token or password is in a template.</p>"
                target.write_text(original, encoding="utf-8")
                answers = [{"path":str(target),"reason":"Homepage candidate"}]
                with patch.object(runner, "eligible_files", return_value=[str(target)]):
                    with patch.object(runner, "groq_json", side_effect=answers) as mock:
                        with self.assertRaisesRegex(runner.WorkerFailure, "Source may contain private data"):
                            runner.plan_and_apply({
                                "projectKey":"website",
                                "instruction":"Continue working on the website navigation",
                            })
                self.assertEqual(mock.call_count,1)
                self.assertEqual(target.read_text(encoding="utf-8"),original)
            finally:
                os.chdir(previous)

    def test_free_worker_with_missing_secrets_never_claims_job(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(runner.WorkerFailure, "Required worker secrets missing"):
                runner.run()

    def test_free_worker_requires_explicit_free_plan_confirmation(self):
        values = {
            "IMPERIAL_WORK_API_URL": "https://example.com", "DEV_WORKER_KEY": "test-only",
            "GROQ_API_KEY": "test-only", "GH_TOKEN": "test-only",
            "GITHUB_REPOSITORY": "imperialauto/shop", "GITHUB_RUN_ID": "123456789",
            "IMPERIAL_FREE_WORKER_ENABLED": "true",
            "GROQ_FREE_PLAN_CONFIRMED": "false",
        }
        with patch.dict(os.environ, values):
            with self.assertRaisesRegex(runner.WorkerFailure, "not explicitly confirmed"):
                runner.run()

    def test_model_call_cap_rejects_third_request_without_network(self):
        with patch.object(runner, "GROQ_REQUESTS", runner.MAX_REQUESTS_PER_JOB):
            with self.assertRaisesRegex(runner.WorkerFailure, "call cap"):
                runner.groq_json("system", "test")

    def test_rate_limit_delay_is_bounded(self):
        self.assertEqual(runner.RateLimited(1).seconds, 60)
        self.assertEqual(runner.RateLimited(99999).seconds, 3600)


class WorkerQueueTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self.old_read_only = app.state.read_only
        app.state.read_only = False
        self.k = patch.object(internal_messaging, "INTERNAL_API_KEY", "test-internal-secret")
        self.k.start()
        self.flags = patch.dict(os.environ, {
            "DEV_COORDINATOR_CAPTURE_ENABLED": "true", "DEV_WORKER_ENABLED": "true",
            "GROQ_FREE_PLAN_CONFIRMED": "true", "DEV_WORKER_KEY": "test-only-worker-key",
        })
        self.flags.start()
        self.c = TestClient(app)
        self.owner = {"X-Internal-Key": "test-internal-secret"}
        self.worker = {"X-Dev-Worker-Key": "test-only-worker-key"}

    def tearDown(self):
        self.flags.stop()
        self.k.stop()
        app.state.read_only = self.old_read_only
        app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()

    def submit(self, id="msg-1", instruction="Continue working on the website acceptance tests"):
        return self.c.post("/internal/imperial/dev-work/submit", headers=self.owner, json={
            "messageId": id, "projectKey": "website",
            "instruction": instruction, "modelPreference": "auto",
        })

    def claim(self, worker="gha-123456789"):
        response = self.c.post("/internal/imperial/dev-work/worker/claim", headers=self.worker,
                               json={"workerId": worker})
        if response.is_success and response.json().get("claimed"):
            self.attempt = response.json()["attempt"]
        return response

    def report(self, work_id, status, worker="gha-123456789", attempt=None, **kwargs):
        return self.c.post(f"/internal/imperial/dev-work/worker/jobs/{work_id}/report",
                           headers=self.worker, json={
                               "workerId": worker, "status": status,
                               "summary": "Synthetic test outcome", "attempt": self.attempt if attempt is None else attempt, **kwargs,
                           })

    @patch.dict(os.environ, {"IMPERIAL_TASKS_ENABLED": "true"})
    def test_intake_creates_one_canonical_task_and_audit(self):
        first = self.submit().json()
        replay = self.submit(instruction="Different instruction on a replay").json()
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["taskId"], first["taskId"])
        self.assertEqual(replay["id"], first["id"])
        with self.Session() as db:
            task = db.query(ImperialTask).one()
            self.assertEqual((task.source, task.external_id), ("dev_work", str(first["id"])))
            self.assertEqual(db.query(ImperialTaskAudit).one().task_id, first["taskId"])

    def test_expired_and_stale_attempts_cannot_report_or_renew(self):
        from datetime import datetime, timedelta, timezone

        self.assertEqual(self.submit().status_code, 200)
        job = self.claim().json()
        job_id = job["id"]
        url = f"/internal/imperial/dev-work/worker/jobs/{job_id}/heartbeat"
        payload = {"workerId": "gha-123456789", "attempt": job["attempt"]}
        self.assertEqual(self.c.post(url, json=payload, headers=self.owner).status_code, 401)
        self.assertEqual(self.c.post(url, json=payload, headers=self.worker).status_code, 200)
        self.assertEqual(self.report(job_id, "failed", attempt=2).status_code, 409)
        with self.Session() as db:
            db.get(DevWorkJob, job_id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.c.post(url, json=payload, headers=self.worker).status_code, 409)
        self.assertEqual(self.report(job_id, "failed").status_code, 409)
        self.assertFalse(self.claim().json()["claimed"])
        with self.Session() as db:
            self.assertEqual(db.get(DevWorkJob, job_id).status, "blocked")

    def test_claim_requires_independent_worker_secret_and_plan_confirmation(self):
        self.assertEqual(self.submit().status_code, 200)
        self.assertEqual(self.c.post("/internal/imperial/dev-work/worker/claim",
                                     json={"workerId": "gha-123456789"}).status_code, 401)
        with patch.dict(os.environ, {"GROQ_FREE_PLAN_CONFIRMED": "false"}):
            self.assertEqual(self.claim().status_code, 503)
        with patch.dict(os.environ, {"DEV_WORKER_ENABLED": "false"}):
            self.assertEqual(self.claim().status_code, 503)
        result = self.claim().json()
        self.assertTrue(result["claimed"])
        self.assertEqual(result["attempt"], 1)
        self.assertFalse(self.claim().json()["claimed"])

    def test_free_worker_skips_finance_and_operations_jobs_without_claiming_them(self):
        for i, key in enumerate(("finance", "operations", "integrations"), start=1):
            result = self.c.post("/internal/imperial/dev-work/submit", headers=self.owner,
                json={"messageId":f"unsupported-{i}", "projectKey":key,
                      "instruction":"Continue working on outstanding "+key+" tasks",
                      "modelPreference":"auto"})
            self.assertEqual(result.status_code,200)
            self.assertEqual(result.json()["status"],"queued")
        self.assertFalse(self.claim().json()["claimed"], "pilot must not claim unsupported jobs")
        self.submit(id="eligible-website")
        picked=self.claim().json()
        self.assertTrue(picked["claimed"])
        self.assertEqual(picked["projectKey"],"website")
        with self.Session() as db:
            other=db.query(DevWorkJob).filter(DevWorkJob.project_key != "website").all()
            self.assertEqual(len(other),3)
            self.assertTrue(all(x.status == "queued" and not x.attempt_count for x in other))

    def test_ready_for_review_job_is_visible_in_owner_dashboard(self):
        saved=self.submit().json()
        self.assertTrue(saved["workerConfigured"])
        claimed=self.claim().json()
        self.assertEqual(claimed["id"],saved["id"])
        self.assertEqual(self.report(claimed["id"],"review_needed",
            resultUrl="https://github.com/imperialauto/shop/pull/990").status_code,200)
        with self.Session() as db:
            snapshot=dev_work.dashboard_snapshot(db,"owner")
            self.assertIn("review_needed",[j["status"] for j in snapshot["jobs"]])
            self.assertTrue(snapshot["workerConfigured"])

    def test_worker_cannot_claim_sensitive_approval_request(self):
        self.assertEqual(self.submit(instruction="Continue working on the website and deploy").json()["status"], "needs_approval")
        self.assertFalse(self.claim().json()["claimed"])

    def test_invalid_and_forged_results_are_rejected(self):
        self.submit()
        job = self.claim().json()
        self.assertEqual(self.report(job["id"], "review_needed").status_code, 422)
        self.assertEqual(self.report(job["id"], "review_needed",
                                     resultUrl="https://github.com/not-ours/repo/pull/1").status_code, 422)
        self.assertEqual(self.report(job["id"], "review_needed",
                                     worker="gha-999999999",
                                     resultUrl="https://github.com/imperialauto/shop/pull/900").status_code, 409)
        good = self.report(job["id"], "review_needed",
                           resultUrl="https://github.com/imperialauto/shop/pull/900")
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["status"], "review_needed")
        self.assertEqual(self.report(job["id"], "failed").status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(DevWorkEvent).count(), 3)
            self.assertEqual(db.query(DevWorkJob).first().result_url,
                             "https://github.com/imperialauto/shop/pull/900")

    def test_owner_notification_outbox_is_at_most_once_and_owner_key_only(self):
        from app.dev_work_model import DevWorkNotice
        with patch.dict(os.environ, {"DEV_WORK_NOTICES_ENABLED": "true"}):
            self.submit()
            job_id = self.claim().json()["id"]
            self.assertEqual(self.report(job_id, "blocked").status_code, 200)
            with self.Session() as db:
                notices = db.query(DevWorkNotice).all()
                self.assertEqual(len(notices), 1)
                self.assertEqual(notices[0].status, "pending")
                self.assertIn(f"development #{job_id}", notices[0].text)

            endpoint = "/internal/imperial/dev-work/notices/claim"
            self.assertEqual(self.c.post(endpoint).status_code, 401)
            claimed = self.c.post(endpoint, headers=self.owner)
            self.assertEqual(claimed.status_code, 200)
            notice = claimed.json()["notice"]
            self.assertEqual(len(notice["claimNonce"]), 32)
            self.assertIsNone(self.c.post(endpoint, headers=self.owner).json()["notice"])

            ack = f"/internal/imperial/dev-work/notices/{notice['id']}/ack"
            self.assertEqual(self.c.post(ack, headers=self.owner, json={
                "claimNonce": "0" * 32, "status": "sent",
            }).status_code, 409)
            confirmed = self.c.post(ack, headers=self.owner, json={
                "claimNonce": notice["claimNonce"], "status": "sent",
            })
            self.assertEqual(confirmed.status_code, 200)
            self.assertEqual(confirmed.json()["status"], "sent")
            self.assertEqual(self.c.post(ack, headers=self.owner, json={
                "claimNonce": notice["claimNonce"], "status": "sent",
            }).status_code, 409)
            with self.Session() as db:
                record = db.query(DevWorkNotice).one()
                self.assertEqual(record.status, "sent")
                self.assertIsNotNone(record.delivered_at)

    def test_owner_notifications_remain_disabled_by_default(self):
        with patch.dict(os.environ, {"DEV_WORK_NOTICES_ENABLED": "false"}):
            self.submit()
            job_id = self.claim().json()["id"]
            self.assertEqual(self.report(job_id, "blocked").status_code, 200)
            self.assertEqual(self.c.post(
                "/internal/imperial/dev-work/notices/claim",
                headers=self.owner,
            ).status_code, 503)

    def test_rate_limit_requeues_with_backoff_and_limits_attempts(self):
        self.submit()
        job_id = self.claim().json()["id"]
        done = self.report(job_id, "rate_limited", retryAfterSeconds=300)
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["status"], "queued")
        self.assertFalse(self.claim().json()["claimed"])
        with self.Session() as db:
            row = db.get(DevWorkJob, job_id)
            row.retry_after = None
            db.commit()
        self.assertEqual(self.claim().json()["attempt"], 2)
        self.assertEqual(self.report(job_id, "rate_limited", retryAfterSeconds=1).status_code, 200)
        with self.Session() as db:
            row = db.get(DevWorkJob, job_id)
            row.retry_after = None
            db.commit()
        self.assertEqual(self.claim().json()["attempt"], 3)
        self.assertEqual(self.report(job_id, "rate_limited", retryAfterSeconds=1).json()["status"], "blocked")
        self.assertFalse(self.claim().json()["claimed"])


if __name__ == "__main__":
    unittest.main()
