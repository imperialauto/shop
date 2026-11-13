"""Disposable PostgreSQL check of the additive Local Node migration and its
database-level approval rules. Runs only against the CI fixture database on
loopback; never Railway, production or user-supplied credentials."""
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

url = make_url(os.environ["TEST_DATABASE_URL"])
assert url.host in ("localhost", "127.0.0.1")
assert url.database == "appointment_test"
engine = create_engine(url)

ROOT = Path(__file__).resolve().parents[1]

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    # Reset only the disposable Local Node tables; never touch existing user data.
    conn.execute(text((ROOT / "migrations/20261111_local_ai_node.down.sql").read_text()))
    if "repair_orders" not in inspect(conn).get_table_names():
        from app.database import Base
        from app import models  # noqa: F401
        Base.metadata.create_all(conn, tables=[Base.metadata.tables[t] for t in ("users", "customers", "vehicles", "repair_orders")])
    migration = (ROOT / "migrations/20261111_local_ai_node.sql").read_text()
    for _ in range(2):  # repeatable
        conn.execute(text(migration))
    names = inspect(conn).get_table_names()
    for table in ("diagnostic_reviews", "diagnostic_review_events", "local_node_heartbeats"):
        assert table in names, table

with engine.begin() as conn:
    user = conn.execute(text("INSERT INTO users (username, password_hash, role, is_active, must_change_password) "
                             "VALUES ('lnode-fixture-' || floor(random()*1e9)::text, 'x', 'tech', true, false) RETURNING id")).scalar_one()
    customer = conn.execute(text("INSERT INTO customers (first_name, last_name) VALUES ('Synthetic', 'Fixture') RETURNING id")).scalar_one()
    vehicle = conn.execute(text("INSERT INTO vehicles (customer_id, year, make, model) VALUES (:c, 2014, 'Ford', 'F-150') RETURNING id"),
                           {"c": customer}).scalar_one()
    ro = conn.execute(text("INSERT INTO repair_orders (customer_id, vehicle_id, status) VALUES (:c, :v, 'diagnosing') RETURNING id"),
                      {"c": customer, "v": vehicle}).scalar_one()


def insert_review(conn, status="queued", **extra):
    columns = {"repair_order_id": ro, "requested_by_user_id": user, "status": status,
               "source_snapshot": "{}", "prompt_version": "fixture", **extra}
    names = ", ".join(columns)
    values = ", ".join(f":{k}" for k in columns)
    return conn.execute(text(f"INSERT INTO diagnostic_reviews ({names}) VALUES ({values}) RETURNING id"), columns).scalar_one()


def must_fail(sql, params=None, label=""):
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params or {})
    except (IntegrityError, DBAPIError):
        return
    raise AssertionError(f"Database accepted a forbidden change: {label}")


def must_fail_insert(label, **kwargs):
    try:
        with engine.begin() as conn:
            insert_review(conn, **kwargs)
    except (IntegrityError, DBAPIError):
        return
    raise AssertionError(f"Database accepted a forbidden insert: {label}")


# Nobody can insert a pre-approved review.
must_fail_insert("insert approved", status="approved", reviewed_by_user_id=user, approved_text="x")
must_fail_insert("insert draft", status="draft_ready")

with engine.begin() as conn:
    review = insert_review(conn)
    conn.execute(text("INSERT INTO diagnostic_review_events (review_id, actor, action) VALUES (:r, 'user:1', 'requested')"), {"r": review})

# One active review per job.
must_fail_insert("second active review")

# Cannot jump from queued straight to approved.
must_fail("UPDATE diagnostic_reviews SET status='approved', reviewed_by_user_id=:u, reviewed_at=now(), approved_text='x' WHERE id=:r",
          {"u": user, "r": review}, "queued -> approved")

with engine.begin() as conn:
    conn.execute(text("UPDATE diagnostic_reviews SET status='draft_ready', draft_json='{}' WHERE id=:r"), {"r": review})

# Approval needs reviewer and text.
must_fail("UPDATE diagnostic_reviews SET status='approved' WHERE id=:r", {"r": review}, "approve without reviewer")
must_fail("UPDATE diagnostic_reviews SET status='approved', reviewed_by_user_id=:u, reviewed_at=now() WHERE id=:r",
          {"u": user, "r": review}, "approve without text")

with engine.begin() as conn:
    conn.execute(text("UPDATE diagnostic_reviews SET status='approved', reviewed_by_user_id=:u, reviewed_at=now(), "
                      "approved_text='Technician-approved text' WHERE id=:r"), {"u": user, "r": review})

# Final rows are immutable; audit history is append-only; reviews are never deleted.
must_fail("UPDATE diagnostic_reviews SET approved_text='changed' WHERE id=:r", {"r": review}, "edit approved")
must_fail("DELETE FROM diagnostic_reviews WHERE id=:r", {"r": review}, "delete review")
must_fail("UPDATE diagnostic_review_events SET action='x' WHERE review_id=:r", {"r": review}, "edit audit")
must_fail("DELETE FROM diagnostic_review_events WHERE review_id=:r", {"r": review}, "delete audit")

# After the decision, a new review for the same job may be requested.
with engine.begin() as conn:
    insert_review(conn)

print("Local Node migration: repeatable; approvals, finality and append-only audit enforced by PostgreSQL.")
