"""Disposable PostgreSQL check of the additive Ask Imperial migration. CI fixture database on
loopback only; never Railway, production or user-supplied credentials."""
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
TABLES = ("assist_requests", "assist_events", "assist_labor_evidence", "shop_rules")

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.execute(text((ROOT / "migrations/20261116_ask_imperial.down.sql").read_text()))  # disposable tables only
    if "repair_orders" not in inspect(conn).get_table_names():
        from app.database import Base
        from app import models  # noqa: F401
        Base.metadata.create_all(conn, tables=[Base.metadata.tables[t] for t in ("users", "customers", "vehicles", "repair_orders")])
    migration = (ROOT / "migrations/20261116_ask_imperial.sql").read_text()
    for _ in range(2):  # safe to run twice
        conn.execute(text(migration))
    schema = inspect(conn)
    for table in TABLES:
        assert table in schema.get_table_names(), table
    # The app's models and the migration agree column for column
    from app.database import Base
    import app.assist_model  # noqa: F401
    for table in TABLES:
        model = set(Base.metadata.tables[table].columns.keys())
        db = {c["name"] for c in schema.get_columns(table)}
        assert model == db, (table, model ^ db)
    indexes = {i["name"] for i in schema.get_indexes("assist_requests")}
    assert "uq_assist_one_open_per_ro" in indexes, indexes

with engine.begin() as conn:
    user = conn.execute(text("INSERT INTO users (username, password_hash, role, is_active, must_change_password) "
                             "VALUES ('ask-fixture-' || floor(random()*1e9)::text, 'x', 'office', true, false) RETURNING id")).scalar_one()
    customer = conn.execute(text("INSERT INTO customers (first_name, last_name) VALUES ('Synthetic', 'Fixture') RETURNING id")).scalar_one()
    vehicle = conn.execute(text("INSERT INTO vehicles (customer_id, year, make, model) VALUES (:c, 2022, 'Dodge', 'Charger') "
                                "RETURNING id"), {"c": customer}).scalar_one()
    ro = conn.execute(text("INSERT INTO repair_orders (customer_id, vehicle_id, status) VALUES (:c, :v, 'diagnosing') RETURNING id"),
                      {"c": customer, "v": vehicle}).scalar_one()

INSERT = ("INSERT INTO assist_requests (requested_by_user_id, repair_order_id, request_text, source_snapshot, "
          "base_fingerprint, prompt_version, status) VALUES (:u, :r, 'estimate brakes', '{}', 'f', 'v', :s) RETURNING id")


def expect_error(statement, params, kind):
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params)
    except kind:
        return
    raise AssertionError(f"expected {kind.__name__}: {statement}")


with engine.begin() as conn:
    first = conn.execute(text(INSERT), {"u": user, "r": ro, "s": "queued"}).scalar_one()
# One open draft per job
expect_error(INSERT, {"u": user, "r": ro, "s": "needs_labor"}, IntegrityError)
expect_error(INSERT, {"u": user, "r": ro, "s": "approved"}, IntegrityError)  # not a valid status
with engine.begin() as conn:
    conn.execute(text("UPDATE assist_requests SET status = 'applied' WHERE id = :i"), {"i": first})
    second = conn.execute(text(INSERT), {"u": user, "r": ro, "s": "queued"}).scalar_one()  # applied frees the slot
    conn.execute(text("INSERT INTO assist_events (request_id, repair_order_id, actor, action) VALUES (:i, :r, 'system', 'x')"),
                 {"i": second, "r": ro})
    conn.execute(text("INSERT INTO assist_labor_evidence (request_id, repair_order_id, vehicle_config, operation, source, "
                      "guide_hours, verified_by_user_id) VALUES (:i, :r, '5.7L', 'Pads', 'ALLDATA Repair', 1.6, :u)"),
                 {"i": second, "r": ro, "u": user})
expect_error("INSERT INTO assist_labor_evidence (request_id, repair_order_id, vehicle_config, operation, source, guide_hours, "
             "verified_by_user_id) VALUES (:i, :r, 'x', 'y', 'ALLDATA Repair', 0, :u)", {"i": second, "r": ro, "u": user},
             IntegrityError)
# History and evidence can't be deleted
expect_error("DELETE FROM assist_events WHERE request_id = :i", {"i": second}, DBAPIError)
expect_error("DELETE FROM assist_labor_evidence WHERE request_id = :i", {"i": second}, DBAPIError)
print("ask imperial migration: ok")
