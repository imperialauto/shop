"""First-party, step-level customer journey metrics. No form values or private URLs."""
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, func, inspect

from app.config import SECRET_KEY
from app.database import Base

SID = re.compile(r"^[a-f0-9]{32}$")
PAGES = frozenset({
    "home", "services", "service", "book", "estimate", "roadside",
    "status_lookup", "status", "approval", "received", "contact",
    "pricing", "faq", "rentals",
})
EVENTS = frozenset({
    "view", "focus", "invalid", "submit", "error", "complete",
    "next", "choice", "approval_complete",
})
STEPS = frozenset({
    "open", "service", "time", "location", "contact", "vehicle",
    "details", "decision", "signature", "review", "send", "success",
    "invalid", "server_error", "call", "other",
})
SECURE_PAGES = frozenset({"status", "approval"})
RETENTION_DAYS = 30
_last_pruned = 0.0


class JourneyEvent(Base):
    __tablename__ = "journey_events"
    id = Column(Integer, primary_key=True)
    session_id = Column(String(32), nullable=False)
    page = Column(String(24), nullable=False)
    event = Column(String(24), nullable=False)
    step = Column(String(24), nullable=False)
    ro_id = Column(Integer, ForeignKey("repair_orders.id"), nullable=True)
    is_preview = Column(Boolean, nullable=False, default=False, server_default="false")
    occurred_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_journey_events_time", "occurred_at"),
        Index("ix_journey_events_session_time", "session_id", "occurred_at"),
        Index("ix_journey_events_ro_time", "ro_id", "occurred_at"),
    )


def enabled() -> bool:
    """Off unless the additive migration is installed and explicitly enabled."""
    return os.getenv("CUSTOMER_JOURNEY_ENABLED", "false").lower() == "true"


def ready(db) -> bool:
    """Feature stays inert until its approved table is actually present."""
    if not enabled():
        return False
    try:
        return inspect(db.bind).has_table("journey_events")
    except Exception:
        return False


def _signer():
    return URLSafeTimedSerializer(SECRET_KEY, salt="imperial-customer-journey-v1")


def context_for(ro_id: int) -> str:
    return _signer().dumps({"ro": int(ro_id)})


def context_ro_id(signed: str) -> int | None:
    if not isinstance(signed, str) or len(signed) > 512:
        return None
    try:
        value = _signer().loads(signed, max_age=60 * 60 * 6)
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
    ro = value.get("ro") if isinstance(value, dict) else None
    return ro if type(ro) is int and 0 < ro < 2_000_000_000 else None


def valid_sid(sid) -> bool:
    return isinstance(sid, str) and SID.fullmatch(sid) is not None


def prune(db, now: datetime) -> None:
    """Delete old anonymous events once per worker per day; never touch shop data."""
    global _last_pruned
    tick = time.monotonic()
    if tick - _last_pruned < 24 * 60 * 60:
        return
    db.query(JourneyEvent).filter(
        JourneyEvent.occurred_at < now - timedelta(days=RETENTION_DAYS)
    ).delete(synchronize_session=False)
    _last_pruned = tick


def record(db, sid: str, page: str, event: str, step: str,
           *, ro_id: int | None = None, is_preview: bool = False) -> bool:
    """Allowlisted, bounded append. Does not persist IP, user agent, URLs or form contents."""
    if not ready(db) or not valid_sid(sid):
        return False
    if page not in PAGES or event not in EVENTS or step not in STEPS:
        return False
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    # At most 200 events per anonymous tab/day; avoid unbounded client submissions.
    count = db.query(func.count(JourneyEvent.id)).filter(
        JourneyEvent.session_id == sid, JourneyEvent.occurred_at >= cutoff
    ).scalar()
    if count >= 200:
        return False
    prune(db, now)
    db.add(JourneyEvent(session_id=sid, page=page, event=event, step=step,
                        ro_id=ro_id, is_preview=is_preview, occurred_at=now))
    return True


def report(db, *, days: int = 7, show_preview: bool = False) -> dict:
    """Aggregate funnel and possible pauses without exposing raw customer data."""
    now = datetime.now(timezone.utc)
    after = now - timedelta(days=days)
    events = db.query(JourneyEvent).filter(
        JourneyEvent.occurred_at >= after, JourneyEvent.is_preview.is_(show_preview)
    ).order_by(JourneyEvent.occurred_at.desc()).limit(10001).all()
    truncated = len(events) > 10000
    events = list(reversed(events[:10000]))
    summary = {}
    for page in ("book", "estimate", "roadside", "status_lookup", "status", "approval"):
        rows = [e for e in events if e.page == page]
        summary[page] = {
            event: len({e.session_id for e in rows if e.event == event})
            for event in ("view", "focus", "invalid", "submit", "error", "complete", "next", "approval_complete")
        }
    sessions = defaultdict(list)
    for e in events:
        sessions[e.session_id].append(e)
    recent = []
    labels = {"view": "Viewed", "focus": "Working on", "invalid": "Validation blocked",
              "submit": "Submitted", "error": "Form error", "complete": "Completed",
              "next": "Continued", "choice": "Selected", "approval_complete": "Approval signed"}
    local = ZoneInfo("America/Phoenix")
    for sid, rows in sessions.items():
        last = rows[-1]
        ro_id = next((e.ro_id for e in reversed(rows) if e.ro_id), None)
        # Completion on an earlier flow must not hide a later stuck approval/form.
        current_flow = [e for e in rows if e.page == last.page]
        completed = any(e.event in ("complete", "approval_complete") for e in current_flow)
        age_minutes = (now - _aware(last.occurred_at)).total_seconds() / 60
        friction = ""
        if not completed and age_minutes >= 10:
            if any(e.event in ("invalid", "error") for e in current_flow):
                friction = "Validation / form error"
            elif last.page == "approval":
                friction = "Approval not finished"
            elif last.event in ("focus", "submit"):
                friction = "Form may be abandoned"
            elif last.event == "next":
                friction = "Clicked through, next step unseen"
        compact = []
        for e in rows:
            label = f"{labels[e.event]} {e.page.replace('_', ' ')}"
            if e.event in ("focus", "invalid", "choice"):
                label += f" · {e.step}"
            if not compact or compact[-1] != label:
                compact.append(label)
        recent.append({
            "visitor": sid[-6:], "ro_id": ro_id, "last_at": _aware(last.occurred_at).astimezone(local),
            "friction": friction, "completed": completed,
            "steps": compact[-12:],
        })
    recent.sort(key=lambda s: s["last_at"], reverse=True)
    return {"rows": summary, "visitors": len(sessions), "recent": recent[:50],
            "possible_pauses": sum(bool(s["friction"]) for s in recent),
            "truncated": truncated, "days": days, "preview": show_preview}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
