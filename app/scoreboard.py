"""Home's daily scoreboard: money in today against the daily goal, the week so far, and hours clocked today.

Money in is the one figure Home, the Money page and the shop TV all use (app/routes/finance.py sales_in), by
Phoenix day:
* payments recorded on job invoices (Payment.paid_at), cash, card, Zelle and the rest;
* job deposits taken before the job has an invoice (JobDeposit.received_at), not removed and not yet moved onto
  an invoice (once moved, a deposit is a Payment dated when it came in, so nothing counts twice);
* business money marked "Got it" on the Money page (FinanceIncoming received that day).
Invoices sent but not paid, estimates and approvals are not money in and are not counted.

The goal is the owner's fixed daily goal (setting daily_sales_goal, $500 unless changed) unless he picked
"what the bills need" on /board/manage (setting daily_goal_mode), the same choice Home's goal bar and the TV use.
"""
from datetime import date, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import finance_plan as plan
from app import shop_board
from app.appointments import PHOENIX


def _money_in(db: Session, start: date, end_exclusive: date) -> float:
    from app.routes import finance  # late import: finance pulls in the web routes

    try:
        return finance.sales_in(db, start, end_exclusive)
    except SQLAlchemyError:  # Money page tables not set up: job payments and deposits still count
        db.rollback()
        return round(finance._shop_payments(db, start, end_exclusive) + finance._open_deposits(db, start, end_exclusive), 2)


def _percent(part: float, whole: float) -> int:
    return max(0, min(100, int(part * 100 / whole))) if whole else 0


def scoreboard(db: Session, now: datetime | None = None) -> dict:
    """Everything the scoreboard partial (_scoreboard.html) shows. Owner-only money: check the role before showing it."""
    from app.routes import finance

    now = (now or shop_board.now_local()).astimezone(PHOENIX)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    week_start = today - timedelta(days=today.weekday())  # Monday
    fixed = finance.daily_goal()
    mode = finance.goal_mode()
    need = None
    if mode == "bills" and today == finance.phoenix_today():
        bar = finance.home_goal(db)  # what the bills need a day, the same as the Money page
        need = bar["need"] if bar else None
    use_bills = bool(need and need > 0)
    goal = need if use_bills else fixed
    came_in = _money_in(db, today, tomorrow)
    week = _money_in(db, week_start, tomorrow)
    days_so_far = plan.working_days(week_start, today)
    week_goal = round(goal * days_so_far, 2)
    hours = None
    try:
        hours = round(shop_board.clocked_seconds_on(shop_board.job_clocks(db), today, now) / 3600, 1)
    except SQLAlchemyError:
        db.rollback()
    return {
        "today": round(came_in, 2), "goal": goal, "left": round(max(goal - came_in, 0), 2), "hit": came_in >= goal,
        "percent": _percent(came_in, goal),
        "source": "bills" if use_bills else "fixed", "mode": mode, "fixed_goal": fixed, "bills_need": need,
        "goal_label": ("what the bills need a day" if use_bills else "your daily goal"),
        "week": round(week, 2), "week_goal": week_goal, "week_percent": _percent(week, week_goal),
        "days_so_far": days_so_far, "hours_today": hours,
    }
