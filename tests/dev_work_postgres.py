"""Disposable PostgreSQL migration contract test for Imperial development work.

Runs ONLY against TEST_DATABASE_URL supplied by CI. Never reads DATABASE_URL
or Railway secrets, and refuses known production database names/hosts.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2
from psycopg2.extensions import parse_dsn
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

url = os.environ.get("TEST_DATABASE_URL", "")
dsn = parse_dsn(url)
if "hostaddr" in dsn or "service" in dsn or any(dsn.get(key) != value for key, value in {
    "dbname": "appointment_test", "host": "127.0.0.1", "user": "test",
}.items()):
    raise SystemExit("Refusing migration test without disposable localhost appointment_test")

root = Path(__file__).resolve().parents[1]
foundation = (root / "migrations/20261110_dev_work_jobs.sql").read_text()
worker = (root / "migrations/20261110_dev_work_runner.sql").read_text()
down_worker = (root / "migrations/20261110_dev_work_runner.down.sql").read_text()
down_foundation = (root / "migrations/20261110_dev_work_jobs.down.sql").read_text()

conn = psycopg2.connect(url)
conn.autocommit = True
with conn:
    with conn.cursor() as cur:
        # Isolation: only the dedicated synthetic development tables.
        cur.execute("DROP TABLE IF EXISTS dev_work_notices")
        cur.execute("DROP TABLE IF EXISTS dev_work_events")
        cur.execute("DROP TABLE IF EXISTS dev_work_jobs")
        try:
            cur.execute(foundation)
            cur.execute(worker)
            cur.execute(worker)  # Idempotent on clean test Postgres

            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='dev_work_jobs'
            """)
            columns = {row[0] for row in cur.fetchall()}
            assert {"attempt_count", "lease_until", "retry_after", "assigned_worker",
                    "model_preference", "status", "source", "external_id"} <= columns
            cur.execute("""
                INSERT INTO dev_work_jobs (source, external_id, project_key, instruction)
                VALUES ('quo_owner', 'synthetic-1', 'website', 'Test no duplicate worker claims')
                RETURNING id, status, attempt_count
            """)
            job_id, status, attempt = cur.fetchone()
            assert (status, attempt) == ("queued", 0)
            cur.execute("""
                INSERT INTO dev_work_jobs (source, external_id, project_key, instruction)
                VALUES ('quo_owner', 'synthetic-1', 'website', 'Should not create duplicate')
                ON CONFLICT (source, external_id) DO NOTHING RETURNING id
            """)
            assert cur.fetchone() is None, "Quo message idempotency constraint missing"

            cur.execute("""
                INSERT INTO dev_work_events (job_id, actor, action, detail)
                VALUES (%s, 'worker', 'blocked', 'synthetic event') RETURNING id
            """, (job_id,))
            event_id = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO dev_work_notices (event_id, job_id, text)
                VALUES (%s, %s, 'Synthetic blocked task notification')
                RETURNING status
            """, (event_id, job_id))
            assert cur.fetchone()[0] == "pending"
            cur.execute("SELECT COUNT(*) FROM dev_work_notices")
            assert cur.fetchone()[0] == 1

            # Commit the fixture so separate coordinator sessions can see it.
            conn.commit()
            engine = create_engine(url)
            try:
                with patch.dict(os.environ, {
                    "DATABASE_URL": "sqlite://",
                    "SECRET_KEY": "synthetic-coordinator-key",
                    "DEV_WORK_NOTICES_ENABLED": "false",
                }):
                    from app.routes.dev_work import (
                        WorkerClaim, WorkerReport, claim_worker_job, report_worker_job,
                    )
                    from app.dev_work_model import DevWorkJob
                    from fastapi import HTTPException

                    worker_id = "synthetic-worker"
                    with Session(engine) as db:
                        claim = claim_worker_job(WorkerClaim(workerId=worker_id), db, True)
                        assert claim["id"] == job_id and claim["attempt"] == 1
                        report = WorkerReport(workerId=worker_id, status="rate_limited",
                                              summary="Synthetic backoff", retryAfterSeconds=300)
                        assert report_worker_job(job_id, report, db, True)["status"] == "queued"
                    with Session(engine) as db:
                        assert not claim_worker_job(WorkerClaim(workerId=worker_id), db, True)["claimed"]
                        db.get(DevWorkJob, job_id).retry_after = datetime.now(timezone.utc) - timedelta(seconds=1)
                        db.commit()
                        assert claim_worker_job(WorkerClaim(workerId=worker_id), db, True)["attempt"] == 2
                        db.get(DevWorkJob, job_id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
                        db.commit()
                    with Session(engine) as db:
                        try:
                            report_worker_job(job_id, report, db, True)
                        except HTTPException as exc:
                            assert exc.status_code == 409
                            db.rollback()
                        else:
                            raise AssertionError("Expired worker report was accepted")
                        assert not claim_worker_job(WorkerClaim(workerId=worker_id), db, True)["claimed"]
                    with Session(engine) as db:
                        assert db.get(DevWorkJob, job_id).status == "blocked"
            finally:
                engine.dispose()

            cur.execute(down_worker)
            cur.execute(down_foundation)
            cur.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_name IN ('dev_work_jobs', 'dev_work_events', 'dev_work_notices')
            """)
            assert cur.fetchall() == []
            print("Development worker additive and rollback migrations PASS on synthetic local PostgreSQL")
        finally:
            # Never operate on any other table or production host.
            cur.execute("DROP TABLE IF EXISTS dev_work_notices")
            cur.execute("DROP TABLE IF EXISTS dev_work_events")
            cur.execute("DROP TABLE IF EXISTS dev_work_jobs")
conn.close()
