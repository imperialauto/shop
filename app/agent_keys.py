"""Assistant access keys: let an AI assistant (Claude) update the dashboard when the owner asks.

The owner issues a key on the Settings page. The plain key is shown once and
never stored: the database keeps only its SHA-256 hash and a short prefix so the
owner can tell keys apart. A key:

* reaches only /internal/agent/v1 (see app/routes/agent_api.py), never a staff page;
* carries named scopes, so it can be limited to reading, or to some kinds of writes;
* expires, and the owner can revoke it at any time from Settings;
* is recorded on every call: last used time on the key, and one row per write in
  agent_actions, so the owner can see exactly what the assistant changed.

The feature is off until AGENT_API_ENABLED=true and the migration
migrations/20261119_agent_keys.sql has been applied (owner approval required).
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database import Base

PREFIX = "iak_"
SCOPES = {
    "customers:read": "Look up customers and their vehicles",
    "customers:write": "Add customers and vehicles",
    "estimates:read": "Look up jobs and estimate lines",
    "estimates:write": "Start jobs and add estimate lines (drafts only, nothing sent to customers)",
    "appointments:read": "See the appointment calendar",
    "appointments:write": "Book tentative appointments, and cancel or complete visits (nothing sent to customers)",
    "appointments:confirm": "Confirm an appointment when you say so (adds it to your calendar and reminders)",
    "board": "See and update the shop TV: to-do list, timers, cars here and job clocks",
    "estimates:send": "Text a customer their estimate approval link from the shop line (logged on the job)",
}
EXPIRY_DAYS = (30, 90, 180, 365)
DEFAULT_EXPIRY_DAYS = 90
MAX_ACTIVE_KEYS = 3


def utc_now():
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return os.getenv("AGENT_API_ENABLED", "").strip().lower() == "true"


class AgentKey(Base):
    __tablename__ = "agent_keys"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    key_prefix = Column(String(16), nullable=False)
    key_hash = Column(String(64), nullable=False, unique=True)
    scopes = Column(Text, nullable=False)  # space separated, from SCOPES
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    last_used_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    revoked_by_user_id = Column(Integer, ForeignKey("users.id"))

    @property
    def scope_list(self) -> list[str]:
        return [s for s in (self.scopes or "").split() if s in SCOPES]

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "expired" if _aware(self.expires_at) <= utc_now() else "active"


class AgentAction(Base):
    """Append-only record of every change made with an assistant key."""
    __tablename__ = "agent_actions"
    id = Column(Integer, primary_key=True)
    agent_key_id = Column(Integer, ForeignKey("agent_keys.id"), nullable=False, index=True)
    action = Column(String(48), nullable=False)
    target_type = Column(String(32))
    target_id = Column(Integer)
    summary = Column(String(300))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def hash_key(raw: str) -> str:
    # Keys carry 256 bits of randomness, so a fast hash is enough; no salt needed.
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clean_scopes(requested) -> list[str]:
    return [s for s in SCOPES if s in set(requested or ())]


def active_keys(db):
    now = utc_now()
    return [k for k in db.query(AgentKey).filter(AgentKey.revoked_at.is_(None)).all() if _aware(k.expires_at) > now]


def issue(db, owner_id: int, name: str, scopes, days: int = DEFAULT_EXPIRY_DAYS) -> tuple[AgentKey, str]:
    """Create a key. Returns the row and the plain key, which is never stored or logged."""
    chosen = clean_scopes(scopes)
    if not chosen:
        raise ValueError("Choose at least one thing the key may do.")
    if days not in EXPIRY_DAYS:
        raise ValueError("Choose how long the key lasts.")
    label = " ".join((name or "").split())[:64] or "Claude"
    if len(active_keys(db)) >= MAX_ACTIVE_KEYS:
        raise ValueError(f"Revoke an old key first. At most {MAX_ACTIVE_KEYS} keys can be active.")
    raw = PREFIX + secrets.token_urlsafe(32)
    key = AgentKey(name=label, key_prefix=raw[:12], key_hash=hash_key(raw), scopes=" ".join(chosen),
                   created_by_user_id=owner_id, expires_at=utc_now() + timedelta(days=days))
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw


def find(db, raw: str | None) -> AgentKey | None:
    """The active key matching the presented value, or None. Constant-time on the hash."""
    if not raw or not raw.startswith(PREFIX) or len(raw) > 200:
        return None
    digest = hash_key(raw)
    key = db.query(AgentKey).filter(AgentKey.key_hash == digest).first()
    if key is None or not secrets.compare_digest(key.key_hash, digest) or key.status != "active":
        return None
    return key


def revoke(db, key_id: int, owner_id: int) -> bool:
    key = db.get(AgentKey, key_id)
    if key is None or key.revoked_at is not None:
        return False
    key.revoked_at, key.revoked_by_user_id = utc_now(), owner_id
    db.commit()
    return True


def record(db, key: AgentKey, action: str, target_type: str | None = None, target_id: int | None = None,
           summary: str = "") -> None:
    """Add to the caller's transaction; the route commits it together with the change."""
    db.add(AgentAction(agent_key_id=key.id, action=action, target_type=target_type, target_id=target_id,
                       summary=(summary or "")[:300]))


def recent_actions(db, limit: int = 20):
    return db.query(AgentAction).order_by(AgentAction.id.desc()).limit(limit).all()
