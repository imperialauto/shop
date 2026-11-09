"""Disposable PostgreSQL check of the additive Imperial task migration.

Runs only on the CI fixture appointment_test database on loopback. Never points
at Railway, production or user-uploaded database credentials.
"""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

url = make_url(os.environ["TEST_DATABASE_URL"])
assert url.host in ("localhost", "127.0.0.1")
assert url.database == "appointment_test"
engine = create_engine(url)

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    # Reset only the two disposable new tables; never touch existing user data.
    conn.execute(text("DROP TABLE IF EXISTS imperial_task_audit"))
    conn.execute(text("DROP TABLE IF EXISTS imperial_tasks"))
    migration = Path("migrations/20261106_imperial_tasks.sql").read_text()
    for _ in range(2):
        conn.execute(text(migration))
    names = inspect(conn).get_table_names()
    assert "imperial_tasks" in names
    assert "imperial_task_audit" in names

with engine.begin() as conn:
    row = conn.execute(text(
        "INSERT INTO imperial_tasks (title, source, external_id, project_key) "
        "VALUES ('Synthetic preview review', 'quo_owner', 'fixture-sms-1', 'website') "
        "RETURNING id"
    )).scalar_one()
    conn.execute(text(
        "INSERT INTO imperial_task_audit (task_id, actor, action, event_id) "
        "VALUES (:task_id, 'owner_sms', 'created', 'fixture-sms-1')"
    ), {"task_id": row})
    assert conn.execute(text("SELECT count(*) FROM imperial_tasks WHERE id=:id"), {"id": row}).scalar_one() == 1
    assert conn.execute(text("SELECT count(*) FROM imperial_task_audit WHERE task_id=:id"), {"id": row}).scalar_one() == 1

try:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO imperial_tasks (title, source, external_id) "
            "VALUES ('Duplicate synthetic task', 'quo_owner', 'fixture-sms-1')"
        ))
    raise AssertionError("Duplicate source event was accepted")
except IntegrityError:
    pass

print("Imperial additive task migration: repeatable, audited and unique on disposable PostgreSQL.")
