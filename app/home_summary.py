"""The numbers at the top of the home screen. Each one links to where you act on it."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import calendar_feed
from app.appointment_model import Appointment
from app.appointments import PHOENIX
from app.dispatch_model import DispatchItem
from app.models import Invoice, LineItem, Payment, RepairOrder

STAGES = (("intake", "Intake"), ("diagnosing", "Diagnosing"), ("waiting_approval", "Waiting approval"),
          ("waiting_parts", "Waiting parts"), ("in_progress", "In progress"), ("complete", "Ready for pickup"))


def summary(db: Session, user, now: datetime | None = None) -> dict:
    now = now or datetime.now(PHOENIX)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        needs_you = db.query(DispatchItem).filter(DispatchItem.status == "needs_owner").count()
        urgent = db.query(DispatchItem).filter(DispatchItem.status == "needs_owner", DispatchItem.urgency != "normal").count()
        booked = db.query(Appointment).filter(Appointment.status.in_(("tentative", "confirmed")),
                                              Appointment.starts_at >= start.astimezone(timezone.utc),
                                              Appointment.starts_at < (start + timedelta(days=1)).astimezone(timezone.utc)).count()
        counts = dict(db.query(RepairOrder.status, func.count()).filter(RepairOrder.status != "delivered").group_by(RepairOrder.status).all())
        awaiting = {row[0] for row in db.query(LineItem.repair_order_id).filter(LineItem.approval == "pending").all()}
        awaiting |= {row[0] for row in db.query(RepairOrder.id).filter(RepairOrder.status == "waiting_approval").all()}
        money = None
        if user.role == "owner":
            open_invoices = db.query(Invoice).filter(Invoice.status.in_(("draft", "sent"))).all()
            paid_on = {inv.id: sum(p.amount for p in db.query(Payment).filter(Payment.invoice_id == inv.id).all()) for inv in open_invoices}
            week = (start - timedelta(days=start.weekday())).astimezone(timezone.utc).replace(tzinfo=None)
            money = {"owed": round(sum(max(inv.total - paid_on[inv.id], 0) for inv in open_invoices), 2),
                     "owed_count": len(open_invoices),
                     "paid_week": round(db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(Payment.paid_at >= week).scalar() or 0, 2)}
    except SQLAlchemyError:
        db.rollback()
        return {"ready": False}
    events = calendar_feed.upcoming(now, days=1)
    today_events = [e for e in (events or []) if e["start"].astimezone(PHOENIX).date() == now.date()] if events is not None else None
    return {"ready": True, "needs_you": needs_you, "urgent": urgent,
            "today": booked + (len(today_events) if today_events else 0), "calendar_ok": today_events is not None,
            "awaiting": len(awaiting),
            "stages": [{"key": k, "label": label, "count": counts.get(k, 0)} for k, label in STAGES],
            "money": money}
