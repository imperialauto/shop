"""Disposable PostgreSQL check of 20261127_mail_inbox.sql: it runs twice, matches app/mail_model.py,
and a pretend mailbox syncs, alerts and lists through it. CI fixture database on loopback only;
never Railway or production. Made-up data only."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("MAIL_TOKEN_KEY", Fernet.generate_key().decode())

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

url = make_url(os.environ["TEST_DATABASE_URL"])
assert url.host in ("localhost", "127.0.0.1")
assert url.database == "appointment_test"
engine = create_engine(url)

import mail_fake  # noqa: E402
from app import mail_model, mail_sync  # noqa: E402
from app.database import Base  # noqa: E402
from app.dispatch_model import DispatchItem  # noqa: E402
from app.mail_model import MailMessage, MailThread  # noqa: E402
from app.models import Customer  # noqa: E402

UP = (ROOT / "migrations/20261127_mail_inbox.sql").read_text()
DOWN = (ROOT / "migrations/20261127_mail_inbox.down.sql").read_text()
MAIL = [Base.metadata.tables[n] for n in mail_model.TABLES]

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.execute(text(DOWN))
    # The tables the new ones point at, as the app's models define them; never the mail tables.
    Base.metadata.create_all(conn, tables=[t for t in Base.metadata.sorted_tables if t not in MAIL])
    for _ in range(2):
        conn.execute(text(UP))
    schema = inspect(conn)
    for table in MAIL:
        have = {c["name"] for c in schema.get_columns(table.name)}
        assert have == set(table.columns.keys()), (table.name, have ^ set(table.columns.keys()))
    indexes = {i["name"] for i in schema.get_indexes("mail_threads")}
    assert {"uq_mail_threads_account_thread", "ix_mail_threads_list"} <= indexes

Session = sessionmaker(bind=engine)
fake = mail_fake.FakeGmail("shop@example-auto.test")
mail_fake.install(fake)
with Session() as db:
    db.query(DispatchItem).filter(DispatchItem.quo_conversation_id.like("WEBMAIL%")).update(
        {"status": "closed"}, synchronize_session=False)  # an earlier run's alerts; mail ids restart below
    cust = Customer(first_name="Pg", last_name="Fixture", email="pg.fixture@example.test", phone="6025550142")
    db.add(cust)
    mail_sync.connect(db, fake.address, "refresh-token", None)
    db.commit()
    fake.add(mail_fake.raw_message("pg1", "tpg1", "noreply@roadside.uhaul.com", "Roadside offer", "Tow"), record=False)
    assert mail_sync.sync(db)["mode"] == "backfill"
    db.commit()
    fake.add(mail_fake.raw_message("pg2", "tpg2", "Pg Fixture <pg.fixture@example.test>", "Ready?", "Is it ready",
                                     when=datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)))
    assert mail_sync.sync(db)["new"] == 1
    db.commit()
    thread = db.query(MailThread).filter_by(gmail_thread_id="tpg2").one()
    assert (thread.customer_id, thread.category, thread.unread) == (cust.id, "leads", True)
    assert db.query(DispatchItem).filter_by(quo_conversation_id=f"WEBMAIL{thread.id}", status="needs_owner").count() == 1
    newest = db.query(MailThread).order_by(MailThread.last_at.desc().nullslast()).first()
    assert newest.gmail_thread_id == "tpg2"
    assert newest.last_at.tzinfo is not None
mail_fake.uninstall()

try:
    with engine.begin() as conn:
        tid = conn.execute(text("SELECT id FROM mail_threads LIMIT 1")).scalar_one()
        conn.execute(text("INSERT INTO mail_messages (thread_id, gmail_message_id) VALUES (:t, 'pg1')"), {"t": tid})
    raise AssertionError("A Gmail message was stored twice")
except IntegrityError:
    pass

with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
    conn.execute(text(DOWN))
    assert not set(mail_model.TABLES) & set(inspect(conn).get_table_names())
    conn.execute(text(UP))

print("Email inbox migration: repeatable, matches the models, syncs and alerts on disposable PostgreSQL.")
