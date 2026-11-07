"""One-shot Railway QA on isolated fictional data, followed by private health server.

This script MUST NOT be attached to Imperial's production Railway project.
It never calls Quo, GitHub, Claude, the customer DB, or outside networks.
It exposes only a minimal health endpoint, not the shop application.
"""
import os
import tempfile
from pathlib import Path

EXPECTED_PROJECT = "c88cae60-111e-46a5-9704-a4c2fe499c7b"
if os.getenv("IMPERIAL_QA_ONLY") != "true":
    raise SystemExit("QA marker missing; refusing startup.")
if os.getenv("RAILWAY_PROJECT_ID") != EXPECTED_PROJECT:
    raise SystemExit("Not in the isolated Imperial preview project; refusing startup.")
if os.getenv("RAILWAY_ENVIRONMENT_NAME") != "preview":
    raise SystemExit("Not in the preview environment; refusing startup.")

# Overwrite, do not inherit, any data or messaging environment configuration.
scratch = tempfile.TemporaryDirectory(prefix="imperial-railway-qa-")
test_db = Path(scratch.name) / "qa.sqlite"
os.environ["DATABASE_URL"] = f"sqlite:///{test_db}"
os.environ["SECRET_KEY"] = "test-only-never-use-for-real-sessions"
os.environ["INTERNAL_API_KEY"] = "synthetic-qa-internal-only"
os.environ["APP_READ_ONLY"] = "false"
os.environ["IMPERIAL_TASKS_ENABLED"] = "true"
os.environ["IMPERIAL_BRAIN_ENABLED"] = "true"
os.environ["OWNER_TEXTS_ENABLED"] = "false"
os.environ["AUTO_REPLIES_ENABLED"] = "false"
os.environ["PROJECT_STATUS_ENABLED"] = "false"
os.environ["PUBLIC_FORMS_ENABLED"] = "false"
for key in ("QUO_API_KEY", "SHOP_APP_API_URL", "PROJECT_STATUS_GITHUB_TOKEN",
            "OPENAI_API_KEY", "GROQ_API_KEY", "STRIPE_SECRET_KEY"):
    os.environ.pop(key, None)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import Base, engine, SessionLocal
from app.imperial_task_model import ImperialTask, ImperialTaskAudit
from app.models import User
from app.auth import hash_password
from app.main import app
from app.routes.imperial_tasks import owner_dashboard_tasks
import uvicorn

checks = []


def check(value, message):
    if not value:
        raise AssertionError(message)
    checks.append(message)


def run_synthetic_tests():
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        session.add(User(
            username="synthetic-owner",
            password_hash=hash_password("local-fixture-owner-only"),
            role="owner",
        ))
        session.commit()

    headers = {"X-Internal-Key": "synthetic-qa-internal-only"}
    # Railway sets Secure session cookies; the isolated test client must
    # use HTTPS semantics or it will drop the owner's authenticated session.
    with TestClient(app, base_url="https://qa.testserver.local") as client:
        health = client.get("/healthz")
        check(health.status_code == 200, "FastAPI started with temporary SQLite")
        check(health.json()["mode"] == "active", "only fixture database allows gated writes")

        refused = client.post("/internal/imperial/tasks/capture", json={
            "messageId": "qa-001", "title": "Synthetic website check",
        })
        check(refused.status_code == 401, "unauthenticated task capture denied")

        payload = {
            "messageId": "qa-owner-message-001",
            "title": "Verify the synthetic website booking form",
            "projectKey": "website",
        }
        first = client.post("/internal/imperial/tasks/capture", headers=headers, json=payload)
        check(first.status_code == 200 and first.json().get("recorded") is True,
              "owner-task capture persisted")
        task_id = first.json()["id"]

        replay = client.post("/internal/imperial/tasks/capture", headers=headers, json=payload)
        check(replay.status_code == 200 and replay.json().get("duplicate") is True
              and replay.json().get("id") == task_id, "same message never creates two tasks")

        queue = client.get("/internal/imperial/tasks/open", headers=headers)
        check(queue.status_code == 200 and len(queue.json().get("tasks", [])) == 1
              and queue.json()["tasks"][0]["id"] == task_id,
              "saved task can be retrieved through internal API")

        login = client.post("/login", data={
            "username": "synthetic-owner",
            "password": "local-fixture-owner-only",
        }, follow_redirects=False)
        check(login.status_code in (302, 303), "fixture owner authenticated")
        dash = client.get("/dashboard")
        check(dash.status_code == 200 and
              "Verify the synthetic website booking form" in dash.text and
              "Saved to-do tasks" in dash.text, "owner dashboard displays same task")

        with SessionLocal() as fresh_session:
            tasks = fresh_session.query(ImperialTask).all()
            audits = fresh_session.query(ImperialTaskAudit).all()
            check(len(tasks) == 1 and len(audits) == 1 and audits[0].task_id == task_id,
                  "new database session finds one task and one audit event")
            check(owner_dashboard_tasks(fresh_session, "office")["enabled"] is False,
                  "non-owner roles cannot read saved-task summary")

        app.state.read_only = True
        blocked = client.post("/internal/imperial/tasks/capture", headers=headers, json={
            "messageId": "qa-owner-message-002",
            "title": "This must never be saved during recovery",
        })
        check(blocked.status_code == 503, "recovery read-only mode rejects task writes")
        app.state.read_only = False
        check(client.get("/internal/imperial/tasks/open").status_code == 401,
              "anonymous task listing denied")
    print("IMPERIAL_QA_PASS: " + str(len(checks)) +
          " isolated checks; fictional records only; no real SMS, no production DB.",
          flush=True)


if __name__ == "__main__":
    run_synthetic_tests()

    health_app = FastAPI(title="Imperial isolated QA health only")

    @health_app.get("/healthz")
    def healthz():
        return {"status": "ok", "mode": "isolated", "passed_checks": len(checks),
                "sms_sent": False, "production_connected": False}

    # No customer, owner login, database or Quo routes are served.
    uvicorn.run(health_app, host="0.0.0.0",
                port=int(os.getenv("PORT", "8080")),
                log_level="warning", access_log=False)
