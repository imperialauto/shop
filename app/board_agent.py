"""Shop TV board tools for the assistant connector (scope "board").

They let the assistant keep the owner's Tasks page and the board in step: list what's on the
board (with when each was ticked off), add a to-do once per Tasks-page task, and tick one off
or bring it back. Every change is logged in agent_actions. Nothing here contacts anyone.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app import agent_keys, shop_board
from app.imperial_task_model import BOARD_CATEGORY, ImperialTask

MAX_LOOKBACK = timedelta(days=14)


class BoardList(BaseModel):
    completed_since: datetime | None = Field(default=None, description="Also list to-dos ticked off since this ISO time. "
                                             "Blank means since midnight today (Phoenix); at most 14 days back.")


class BoardAdd(BaseModel):
    title: str = Field(min_length=2, max_length=200, description="What to do, in a few words. No phone numbers or full customer names: it shows on the shop TV.")
    urgent: bool = Field(default=False, description="Shows in red at the top of the list.")
    task_key: str | None = Field(default=None, max_length=128, description="The Tasks-page task this mirrors. "
                                 "Adding the same key again returns the to-do already on the board.")


class BoardSet(BaseModel):
    todo_id: int | None = Field(default=None, gt=0)
    task_key: str | None = Field(default=None, max_length=128)
    done: bool = Field(description="true ticks it off (moves it to Completed); false puts it back on the list.")

    @model_validator(mode="after")
    def _one(self):
        if not (self.todo_id or self.task_key):
            raise ValueError("Send todo_id or task_key.")
        return self


def _on():
    from app.routes.imperial_tasks import enabled

    if not enabled():
        raise HTTPException(503, "The board's to-do list isn't turned on.")


def list_todos(db: Session, args: BoardList) -> dict:
    _on()
    now = datetime.now(timezone.utc)
    since = args.completed_since or shop_board.now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    since = max(since, now - MAX_LOOKBACK)
    rows = db.query(ImperialTask).filter(ImperialTask.category == BOARD_CATEGORY, ImperialTask.status.in_(shop_board.OPEN_TODO)) \
        .order_by(ImperialTask.id).all()
    done = db.query(ImperialTask).filter(ImperialTask.category == BOARD_CATEGORY, ImperialTask.status == shop_board.DONE,
                                         ImperialTask.updated_at >= since.astimezone(timezone.utc)) \
        .order_by(ImperialTask.updated_at.desc()).limit(200).all()
    return {"open": [shop_board.todo_json(t) for t in rows], "completed": [shop_board.todo_json(t) for t in done],
            "completed_since": since.isoformat()}


def add_todo(db: Session, key, args: BoardAdd) -> dict:
    _on()
    log = lambda task: agent_keys.record(db, key, "board_todo_added", "imperial_task", task.id, task.title)  # noqa: E731
    try:
        task, created = shop_board.add_todo(db, None, args.title, urgent=args.urgent, task_key=args.task_key,
                                            actor=f"agent:{key.id}", log_change=log)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"todo": shop_board.todo_json(task), "created": created}


def set_todo(db: Session, key, args: BoardSet) -> dict:
    _on()
    task = shop_board.find_todo(db, args.todo_id, args.task_key)
    if task is None:
        raise HTTPException(404, "That to-do isn't on the shop board.")
    log = lambda t: agent_keys.record(db, key, "board_todo_done" if args.done else "board_todo_reopened",  # noqa: E731
                                      "imperial_task", t.id, t.title)
    if (task.status == shop_board.DONE) == args.done:
        return {"todo": shop_board.todo_json(task), "changed": False}
    try:
        shop_board.set_done(db, None, task.id, args.done, actor=f"agent:{key.id}", log_change=log)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    db.refresh(task)
    return {"todo": shop_board.todo_json(task), "changed": True}


# ---------------------------------------------------------------- TV timers, cars on the lot, job clocks
# These change only what the shop TV shows (business settings), never a job, and contact no one.

class TimerStart(BaseModel):
    label: str = Field(default="Timer", max_length=60, description="What it's for, in a few words, like "
                       "\"Explorer pressure test\". It shows big on the shop TV, so no phone numbers or full customer names.")
    minutes: int = Field(ge=1, le=shop_board.TIMER_MAX_MINUTES, description="How long, in whole minutes.")


class TimerChange(BaseModel):
    timer_id: str | None = Field(default=None, max_length=16, description="From list_board_timers or start_board_timer.")
    label: str | None = Field(default=None, max_length=60, description="Or the timer's label (or part of it), "
                              "when exactly one timer on the TV matches.")
    action: Literal["stop", "add_5_minutes"] = Field(description="stop takes it off the TV (also silences one that "
                                                     "ran out); add_5_minutes adds five minutes (from now if it ran out).")

    @model_validator(mode="after")
    def _one(self):
        if not (self.timer_id or (self.label or "").strip()):
            raise ValueError("Send timer_id or label.")
        return self


class CarHere(BaseModel):
    job_id: int = Field(gt=0)
    here: bool = Field(description="true: the car is at the shop (shows under Cars here); false: it left.")


class JobClock(BaseModel):
    job_id: int = Field(gt=0)
    running: bool = Field(description="true starts the work clock on the job; false stops it (time worked is kept).")


def _actor(key) -> str:
    return f"agent:{key.id}"


def list_timers(db: Session) -> dict:
    now = shop_board.now_local()
    return {"timers": shop_board.timers(db, now), "max_running": shop_board.TIMER_MAX_RUNNING,
            "note": "The TV beeps when a timer runs out, once someone has tapped the TV page since it last reloaded."}


def start_timer(db: Session, key, args: TimerStart) -> dict:
    agent_keys.record(db, key, "board_timer_started", None, None, f"{args.label} {args.minutes} min")
    try:
        timer = shop_board.start_timer(db, _actor(key), args.label, args.minutes)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None
    db.commit()
    shown = next((t for t in shop_board.timers(db, shop_board.now_local()) if t["id"] == timer["id"]), None)
    return {"timer": shown or {"id": timer["id"], "label": timer["label"], "minutes": timer["minutes"]},
            "note": "It shows on the TV within about 20 seconds."}


def _find_timer(db: Session, args: TimerChange) -> dict:
    running = shop_board.timers(db, shop_board.now_local())
    if args.timer_id:
        found = [t for t in running if t["id"] == args.timer_id]
    else:
        want = " ".join(args.label.lower().split())
        found = [t for t in running if t["label"].lower() == want] or [t for t in running if want in t["label"].lower()]
    if not found:
        raise HTTPException(404, "No timer like that is on the TV. It may have ended or been stopped.")
    if len(found) > 1:
        raise HTTPException(409, "More than one timer matches: " + ", ".join(f"{t['label']} ({t['id']})" for t in found)
                            + ". Send its timer_id.")
    return found[0]


def change_timer(db: Session, key, args: TimerChange) -> dict:
    timer = _find_timer(db, args)
    operation = "stop" if args.action == "stop" else "plus5"
    agent_keys.record(db, key, "board_timer_" + ("stopped" if operation == "stop" else "plus5"), None, None, timer["label"])
    if not shop_board.change_timer(db, _actor(key), timer["id"], operation):
        db.rollback()
        raise HTTPException(404, "That timer already ended.")
    db.commit()
    return {"changed": timer["label"], "action": args.action, "timers": shop_board.timers(db, shop_board.now_local())}


def list_jobs(db: Session) -> dict:
    """Open jobs as the TV labels them (first name and vehicle), whether the car is here, and time worked vs book."""
    rows = shop_board.lot_list(db)
    keep = ("id", "po", "who", "vehicle", "stage", "here", "book", "worked", "clock_on", "over_book")
    return {"jobs": [{k: row.get(k) for k in keep if k in row} for row in rows]}


def set_here(db: Session, key, args: CarHere) -> dict:
    agent_keys.record(db, key, "board_car_here" if args.here else "board_car_gone", "repair_order", args.job_id)
    if not shop_board.set_here(db, _actor(key), args.job_id, args.here):
        db.rollback()
        raise HTTPException(404, "That job isn't open.")
    db.commit()
    return {"job_id": args.job_id, "here": args.here}


def set_clock(db: Session, key, args: JobClock) -> dict:
    agent_keys.record(db, key, "board_clock_started" if args.running else "board_clock_stopped", "repair_order", args.job_id)
    if not shop_board.set_clock(db, _actor(key), args.job_id, args.running):
        db.rollback()
        raise HTTPException(404, "That job isn't open.")
    db.commit()
    row = next((r for r in shop_board.lot_list(db) if r["id"] == args.job_id), {})
    return {"job_id": args.job_id, "clock_on": row.get("clock_on", args.running), "worked_hours": row.get("worked"),
            "book_hours": row.get("book")}
