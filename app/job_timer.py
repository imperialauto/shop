"""The job page's work clock: each worker clocks in and out on a repair order, and the page shows the hours
clocked against the labor hours on the job (labor lines not declined, quantity is hours).

It is the same clock the shop TV shows (app/shop_board.py, setting "board_job_clock"), so starting it on the job
page also shows it running on the TV, and the TV's "Stop clock" stops everyone on that job."""
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import shop_board
from app.models import RepairOrder, User


def _hours(seconds: int) -> float:
    return round(seconds / 3600, 1)


def _name(user: User | None) -> str:
    if user is None:
        return "Former worker"
    return ((user.full_name or user.username or "").split() or ["Worker"])[0][:24]


def for_job(db: Session, ro: RepairOrder, user: User, now: datetime | None = None) -> dict | None:
    """What the job page's clock shows for this worker. None if the clock can't be read right now."""
    now = (now or shop_board.now_local())
    try:
        clock = shop_board.job_clocks(db).get(ro.id)
        billed = shop_board.book_hours(db, ro.id)
    except SQLAlchemyError:
        db.rollback()
        return None
    me = shop_board.worker_key(user)
    by = clock["by"] if clock else {}
    mine = by.get(me)
    ids = [int(who[1:]) for who in by if who.startswith("u") and who[1:].isdigit()]
    users = {u.id: u for u in db.query(User).filter(User.id.in_(ids))} if ids else {}
    workers = []
    for who, entry in by.items():
        seconds = shop_board.worked_seconds(clock, now, who)
        if not seconds and not entry["on"]:
            continue
        name = "Shop clock" if who == shop_board.SHOP_CLOCK else _name(users.get(int(who[1:])) if who[1:].isdigit() else None)
        workers.append({"name": name, "hours": _hours(seconds), "on": entry["on"] is not None, "me": who == me})
    workers.sort(key=lambda w: (not w["me"], w["name"]))
    clocked = _hours(shop_board.worked_seconds(clock, now))
    return {
        "mine_on": bool(mine and mine["on"]),
        "mine_seconds": shop_board.worked_seconds(clock, now, me) if mine else 0,
        "mine_hours": _hours(shop_board.worked_seconds(clock, now, me)) if mine else 0.0,
        "clocked": clocked,
        "billed": billed,
        "over": bool(billed and clocked > billed),
        "left": round(max(billed - clocked, 0), 1),
        "workers": workers,
        "anyone_on": any(w["on"] for w in workers),
        "can_start": ro.status != "delivered",
    }
