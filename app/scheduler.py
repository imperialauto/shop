"""
Background scheduler — runs periodic jobs inside the FastAPI process.
Jobs:
  - followup_job: daily 9am Phoenix time — texts customers whose estimates are 3+ days old and unscheduled
"""

import json
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler(timezone="America/Phoenix")


async def followup_job():
    """Find stale draft_ready sessions (3+ days, no follow-up sent) and text them."""
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


def start_scheduler():
    scheduler.add_job(followup_job, "cron", hour=9, minute=0, id="followup_job", replace_existing=True)
    scheduler.start()
    print("[Scheduler] Started — follow-up job runs daily at 9:00 AM Phoenix time")
