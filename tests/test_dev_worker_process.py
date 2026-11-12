"""Run a disposable worker process over loopback and interrupt it after claim."""
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from threading import Thread

import uvicorn

import test_free_groq_worker as queue_tests
from app.dev_work_model import DevWorkEvent, DevWorkJob
from app.main import app
from app.routes.dev_work import dashboard_snapshot


WORKER = """
import json
import os
import sys
import time
from urllib.request import Request, urlopen

request = Request(sys.argv[1], data=b'{"workerId":"fixture-windows"}',
                  headers={"Content-Type":"application/json",
                           "X-Dev-Worker-Key":os.environ["FIXTURE_WORKER_KEY"]})
with urlopen(request, timeout=5) as response:
    print(json.dumps(json.load(response)), flush=True)
time.sleep(60)
"""


class WorkerProcessRecoveryTests(queue_tests.unittest.TestCase):
    setUp = queue_tests.WorkerQueueTests.setUp
    tearDown = queue_tests.WorkerQueueTests.tearDown
    submit = queue_tests.WorkerQueueTests.submit
    claim = queue_tests.WorkerQueueTests.claim
    report = queue_tests.WorkerQueueTests.report

    def test_killed_process_cannot_report_and_its_job_is_not_reassigned(self):
        job_id = self.submit().json()["id"]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
            thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
                env["FIXTURE_WORKER_KEY"] = self.worker["X-Dev-Worker-Key"]
                url = f"http://127.0.0.1:{port}/internal/imperial/dev-work/worker/claim"
                with subprocess.Popen([sys.executable, "-I", "-c", WORKER, url],
                                      env=env, stdout=subprocess.PIPE, text=True) as worker:
                    try:
                        claim = json.loads(worker.stdout.readline())
                        self.assertTrue(claim["claimed"])
                        self.assertEqual(claim["id"], job_id)
                        self.assertIsNone(worker.poll())
                    finally:
                        if worker.poll() is None:
                            worker.kill()
                        worker.wait(timeout=5)
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

        self.attempt = claim["attempt"]
        self.assertFalse(self.claim("fixture-second").json()["claimed"])
        with self.Session() as db:
            db.get(DevWorkJob, job_id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.report(job_id, "failed", worker="fixture-windows").status_code, 409)
        self.assertFalse(self.claim("fixture-second").json()["claimed"])
        with self.Session() as db:
            jobs = dashboard_snapshot(db, "owner")["jobs"]
            self.assertEqual(jobs[0]["status"], "blocked")
            self.assertIn("lease expired", jobs[0]["latestSummary"])
            events = db.query(DevWorkEvent).filter_by(job_id=job_id).all()
            self.assertEqual([event.action for event in events].count("claimed"), 1)
            self.assertEqual([event.action for event in events].count("lease_expired"), 1)
