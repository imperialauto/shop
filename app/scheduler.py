"""
Background scheduler — runs periodic jobs inside the FastAPI process.
Jobs:
  - followup_job: daily 9am Phoenix time — texts customers whose estimates are 3+ days old and unscheduled
  - finance_rollover_job: daily 12:15am Phoenix time — adds the next weekly/monthly bill on the Money page once
    a due day passes (owner's own figures only; sends nothing, never marks a bill paid)
"""

import json
import os
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler(timezone="America/Phoenix")


async def followup_job():
    """Find stale draft_ready sessions (3+ days, no follow-up sent) and text them."""
    if os.getenv("LEGACY_SMS_INTAKE_ENABLED") != "true":
        return
    from app.database import SessionLocal
    from app.models import EstimateSession
    from app.routes.webhooks import send_sms

    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=3)
        stale = (
            db.query(EstimateSession)
            .filter(
                EstimateSession.status == "draft_ready",
                EstimateSession.updated_at < cutoff,
                EstimateSession.follow_up_sent == False,  # noqa: E712
            )
            .all()
        )
        print(f"[FollowUp] Checking — {len(stale)} stale estimate(s) to follow up on")

        for session in stale:
            try:
                collected = json.loads(session.collected_data or "{}")
                name = collected.get("name", "there")
                year = collected.get("year", "")
                make = collected.get("make", "")
                model = collected.get("model", "")

                msg = (
                    f"Hey {name}! Just following up — we still have your estimate ready for "
                    f"the {year} {make} {model}. Ready to get it scheduled? "
                    f"Reply YES to book a drop-off or call us at (480) 914-4144. No pressure!"
                )
                await send_sms(session.phone_number, msg)
                session.follow_up_sent = True
                db.commit()
                print(f"[FollowUp] Sent to {session.phone_number} (session {session.id})")
            except Exception as e:
                print(f"[FollowUp] Error on session {session.id}: {e}")
    finally:
        db.close()


async def finance_rollover_job():
    """Rolls repeating Money-page bills forward. Earlier unpaid periods stay open and show as late."""
    from sqlalchemy.exc import SQLAlchemyError
    from app.database import SessionLocal
    from app.routes.finance import phoenix_today, roll_repeating_bills

    db = SessionLocal()
    try:
        added = roll_repeating_bills(db, phoenix_today())
        db.commit()
        print(f"[Finance] Repeating bills rolled forward — {added} added")
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[Finance] Roll-forward skipped: {e.__class__.__name__}")
    finally:
        db.close()


def start_scheduler():
    scheduler.add_job(followup_job, "cron", hour=9, minute=0, id="followup_job", replace_existing=True)
    scheduler.add_job(finance_rollover_job, "cron", hour=0, minute=15, id="finance_rollover_job", replace_existing=True)
    if os.getenv("MAIL_SYNC_ENABLED", "").strip().lower() == "true" and os.getenv("MAIL_INBOX_ENABLED", "").strip().lower() == "true":
        from app.mail_sync import scheduled_sync
        scheduler.add_job(scheduled_sync, "interval", seconds=60, id="mail_sync_job", replace_existing=True,
                          max_instances=1, coalesce=True)
    scheduler.start()
    print("[Scheduler] Started — follow-up job daily at 9:00 AM, bill roll-forward daily at 12:15 AM Phoenix time")
