"""Disposable PostgreSQL check: the original dispatcher constraint rejects 'queued' (the
production bug), and 20261116_dispatch_outbound_queued.sql fixes it, runs twice, and its
down migration restores the old rule. CI fixture database on loopback only."""
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

url = make_url(os.environ["TEST_DATABASE_URL"])
assert url.host in ("localhost", "127.0.0.1")
assert url.database == "appointment_test"
engine = create_engine(url)
ROOT = Path(__file__).resolve().parents[1]
UP = (ROOT / "migrations/20261116_dispatch_outbound_queued.sql").read_text()
DOWN = (ROOT / "migrations/20261116_dispatch_outbound_queued.down.sql").read_text()


def insert(status):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO dispatch_outbound (dedupe_key, kind, status) VALUES "
                          "('pgcheck-' || floor(random()*1e12)::text, 'dashboard_reply', :s)"), {"s": status})


with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.execute(text("DROP TABLE IF EXISTS dispatch_outbound"))
    conn.execute(text("""CREATE TABLE dispatch_outbound (
        id SERIAL PRIMARY KEY, dedupe_key VARCHAR(160) NOT NULL UNIQUE, kind VARCHAR(16) NOT NULL,
        item_id INTEGER, status VARCHAR(12) NOT NULL DEFAULT 'reserved',
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT dispatch_outbound_valid_status CHECK (status IN ('reserved','sent','failed','skipped')))"""))

try:  # production before the fix
    insert("queued")
    raise SystemExit("expected the original constraint to reject 'queued'")
except IntegrityError:
    print("ok: original constraint rejects 'queued' (reproduces production)")

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    for _ in range(2):
        conn.execute(text(UP))
insert("queued")
for status in ("reserved", "sent", "failed", "skipped"):
    insert(status)
try:
    insert("pending")
    raise SystemExit("unknown status must still be rejected")
except IntegrityError:
    pass
print("ok: migration allows 'queued', keeps the others, still rejects unknown statuses, repeatable")

# Hold an outbound row in another transaction while the real queue function runs.
# Null item IDs exercise claiming without creating customer data or sending messages.
import fixture_environment  # noqa: F401
from sqlalchemy.orm import Session
from app.routes.dispatch import queued_replies

with engine.begin() as conn:
    conn.execute(text("DELETE FROM dispatch_outbound WHERE status = 'queued'"))
    conn.execute(text("INSERT INTO dispatch_outbound (dedupe_key, kind, status) VALUES "
                      "('locked-claim', 'dashboard_reply', 'queued'), "
                      "('available-claim', 'dashboard_reply', 'queued')"))
with engine.begin() as holder:
    holder.execute(text("SELECT id FROM dispatch_outbound WHERE dedupe_key = 'locked-claim' FOR UPDATE"))
    with Session(engine) as worker:
        worker.execute(text("SET LOCAL lock_timeout = '1s'"))
        assert queued_replies(worker) == []
    statuses = dict(holder.execute(text("SELECT dedupe_key, status FROM dispatch_outbound "
                                        "WHERE dedupe_key IN ('locked-claim', 'available-claim')")).all())
    assert statuses == {"locked-claim": "queued", "available-claim": "skipped"}, statuses
with Session(engine) as worker:
    assert queued_replies(worker) == []
    assert worker.execute(text("SELECT status FROM dispatch_outbound WHERE dedupe_key = 'locked-claim'")).scalar_one() == "skipped"
print("ok: a competing tick skips a locked row and claims the available row")
with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.execute(text(DOWN))
try:
    insert("queued")
    raise SystemExit("down migration should restore the old rule")
except IntegrityError:
    print("ok: down migration restores the old rule")
with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.execute(text("DROP TABLE dispatch_outbound"))
