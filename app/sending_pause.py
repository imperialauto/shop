"""The owner's STOP ALL switch. The newest audit row decides, so history is kept."""
from datetime import datetime

from sqlalchemy.orm import Session

from app import business_settings
from app.database import SessionLocal
from app.dispatch_model import DispatchAudit, DispatchOutbound

PAUSED = "sending_paused"
RESUMED = "sending_resumed"
DASHBOARD_REPLY = "dashboard_reply"


def is_paused(db: Session) -> bool:
    row = db.query(DispatchAudit.action).filter(DispatchAudit.action.in_((PAUSED, RESUMED))) \
        .order_by(DispatchAudit.id.desc()).first()
    return bool(row) and row[0] == PAUSED


def last_resume_cycle(db: Session) -> tuple[int, datetime] | None:
    stopped = db.query(DispatchAudit.id).filter_by(action=PAUSED).order_by(DispatchAudit.id.desc()).first()
    if stopped is None:
        return None
    resumed = db.query(DispatchAudit.created_at, DispatchAudit.detail).filter(
        DispatchAudit.action == RESUMED, DispatchAudit.id > stopped.id,
    ).order_by(DispatchAudit.id).first()
    return (stopped.id, resumed.created_at) if resumed and resumed.detail == "owner_summary" else None


def pause(db: Session, actor: str) -> int:
    """Stops sending. Replies waiting to go out are cancelled, never sent later. Returns how many."""
    waiting = db.query(DispatchOutbound).filter(DispatchOutbound.kind == DASHBOARD_REPLY, DispatchOutbound.status == "queued").all()
    for row in waiting:
        row.status = "skipped"
    db.add(DispatchAudit(item_id=None, actor=actor, action=PAUSED, detail=f"cancelled {len(waiting)} queued"))
    db.commit()
    return len(waiting)


def resume(db: Session, actor: str) -> None:
    detail = "owner_summary" if business_settings.raw("owner_alert_resume_mode") == "summary" else None
    db.add(DispatchAudit(item_id=None, actor=actor, action=RESUMED, detail=detail))
    db.commit()


def sending_allowed() -> bool:
    """For code with no database session. If the switch can't be read, nothing is sent."""
    db = SessionLocal()
    try:
        return not is_paused(db)
    except Exception as err:
        print(f"[SMS BLOCKED] Could not read the stop switch: {type(err).__name__}")
        return False
    finally:
        db.close()
