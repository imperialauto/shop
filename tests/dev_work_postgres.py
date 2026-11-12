"""Disposable PostgreSQL migration contract test for Imperial development work.

Runs ONLY against TEST_DATABASE_URL supplied by CI. Never reads DATABASE_URL
or Railway secrets, and refuses known production database names/hosts.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
import psycopg2
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

url = os.environ.get("TEST_DATABASE_URL", "")
if not url:
    raise SystemExit("Refusing migration test without disposable localhost appointment_test")
target = make_url(url)
if (target.get_backend_name(), target.host, target.database, target.username) != (
    "postgresql", "127.0.0.1", "appointment_test", "test",
) or target.query:
    raise SystemExit("Refusing migration test without disposable localhost appointment_test")


def verify_competing_claims(job_id):
    from app.routes.dev_work import WorkerClaim, claim_worker_job

    engine = create_engine(target, connect_args={"options": "-c statement_timeout=5000"})
    before_commit = Event()
    release = Event()

    def hold_claim(session):
        # Flush the update and event while the first transaction still holds its lock.
        session.flush()
        before_commit.set()
        assert release.wait(8), "First claim did not receive release signal"

    def first_claim():
        with Session(engine) as session:
            event.listen(session, "before_commit", hold_claim, once=True)
            return claim_worker_job(WorkerClaim(workerId="worker-first"), session, True)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(first_claim)
            try:
                assert before_commit.wait(8), "First claim did not acquire and hold its row lock"
                with Session(engine) as session:
                    second = claim_worker_job(WorkerClaim(workerId="worker-second"), session, True)
                assert second["claimed"] is False
            finally:
                release.set()
            result = first.result(timeout=8)
        assert result["claimed"] is True and result["id"] == job_id and result["attempt"] == 1
    finally:
        engine.dispose()


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

            verify_competing_claims(job_id)
            cur.execute("SELECT status, assigned_worker, attempt_count FROM dev_work_jobs WHERE id = %s", (job_id,))
            assert cur.fetchone() == ("running", "worker-first", 1)
            cur.execute("SELECT COUNT(*) FROM dev_work_events WHERE job_id = %s AND action = 'claimed'", (job_id,))
            assert cur.fetchone() == (1,)
            print("Competing development workers produce exactly one PostgreSQL claim")

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
