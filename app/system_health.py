"""One line on Home: is everything Imperial depends on working? (R38)

Only problems are spelled out; when all is well it says so in a few words.
Every check reads records the system already keeps, so a check can't break
anything, and a check that itself fails is reported rather than hidden.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app import name_finder
from app.dispatch_model import OPEN_STATUSES, DispatchItem, DispatchOutbound, DispatchSync

MESSAGING_SILENT_AFTER = timedelta(minutes=20)  # the messaging service checks in every 5 minutes
BACKUP_STALE_AFTER = timedelta(hours=30)
DRAFT_LATE_AFTER = timedelta(minutes=15)
CUSTOMER_SEND_LOOKBACK = timedelta(hours=72)


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _check_messaging(db: Session, now: datetime) -> dict:
    row = db.get(DispatchSync, "quo_recovery")
    last = _utc(row.last_success_at) if row else None
    if last is None or now - last > MESSAGING_SILENT_AFTER:
        return {"name": "Quo texts and calls", "ok": False,
                "detail": "The messaging service hasn't checked in" + (f" since {last.strftime('%-I:%M %p')} UTC" if last else "") + ".",
                "fix": "Review new customer messages in Quo and check the messaging-service health in Railway."}
    if (row.consecutive_failures or 0) >= 3:
        return {"name": "Quo texts and calls", "ok": False, "detail": f"{row.consecutive_failures} checks in a row had errors.",
                "fix": "Review the messaging-service logs in Railway and check for missed customer intake."}
    return {"name": "Quo texts and calls", "ok": True, "detail": "Checking in every few minutes."}


def _check_backup(db: Session, now: datetime) -> dict:
    row = db.get(DispatchSync, "backup")
    last = _utc(row.last_success_at) if row else None
    if last is None or (row.consecutive_failures or 0) >= 1 or now - last > BACKUP_STALE_AFTER:
        return {"name": "Nightly backup", "ok": False, "detail": "Last night's backup didn't finish.",
                "fix": "Review the backup job, verify the latest usable backup, and delay database changes until resolved."}
    return {"name": "Nightly backup", "ok": True, "detail": f"Last good copy {last.strftime('%b %-d')}."}


def _check_drafts(db: Session, now: datetime) -> dict:
    waiting = [i for i in db.query(DispatchItem).filter(DispatchItem.status == "needs_owner").all()
               if i.draft_reply is None and _utc(i.updated_at) and now - _utc(i.updated_at) > DRAFT_LATE_AFTER]
    if waiting:
        return {"name": "AI reply drafts", "ok": False,
                "detail": f"{len(waiting)} customer message{'s are' if len(waiting) != 1 else ' is'} still waiting for a draft.",
                "fix": "Reply manually in Quo if needed, then check the AI draft worker and retry only after reviewing the thread."}
    return {"name": "AI reply drafts", "ok": True, "detail": "Drafts are arriving."}




def _check_customer_sends(db: Session, now: datetime) -> dict:
    """Owner-approved customer texts with unsuccessful outbound results.

    Only explicit customer replies are counted; auto replies disabled by policy
    and owner alert SMS are not customer-send failures. Quo API acceptance is
    not proof of handset delivery, so a 'sent' result is not a delivery receipt.
    """
    recent = db.query(DispatchOutbound).filter(
        DispatchOutbound.updated_at >= now - CUSTOMER_SEND_LOOKBACK,
        DispatchOutbound.status.in_(("failed", "skipped")),
        or_(
            DispatchOutbound.kind == "dashboard_reply",
            and_(DispatchOutbound.kind == "customer",
                 DispatchOutbound.dedupe_key.like("reply:%")),
        ),
    ).all()
    failed = sum(row.status == "failed" for row in recent)
    skipped = sum(row.status == "skipped" for row in recent)
    if not recent:
        return {"name": "Customer text sends", "ok": True,
                "detail": "No failed or skipped owner-approved customer sends in the past 72 hours."}
    breakdown = ", ".join(
        part for part in (f"{failed} failed" if failed else "",
                          f"{skipped} skipped" if skipped else "") if part
    )
    return {"name": "Customer text sends", "ok": False,
            "detail": f"{len(recent)} owner-approved customer text send(s) need review in the past 72 hours ({breakdown}).",
            "fix": "Review the latest customer threads in Quo before replying manually. Do not resend automatically."}


def _check_names(db: Session) -> dict | None:
    left = name_finder.progress(db)
    if not left["unnamed"]:
        return None
    return {"name": "Customer names", "ok": True, "info": True,
            "detail": f"{left['unnamed']} without a name" + (f", {left['waiting']} still to look up in Quo" if left["waiting"] else "") + "."}


def report(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    checks = []
    for check in (_check_messaging, _check_backup, _check_drafts, _check_customer_sends):
        try:
            checks.append(check(db, now))
        except Exception:  # a broken check is itself a problem worth showing
            db.rollback()
            checks.append({"name": check.__name__.replace("_check_", "").capitalize(), "ok": False,
                           "detail": "This check couldn't run.", "fix": "Review the health-check and database logs before trusting the status."})
    try:
        names = _check_names(db)
        if names:
            checks.append(names)
    except Exception:
        db.rollback()
    problems = [c for c in checks if not c["ok"]]
    return {"ok": not problems, "problems": problems, "checks": checks}
