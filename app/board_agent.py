"""Shop TV board tools for the assistant connector (scope "board").

They let the assistant keep the owner's Tasks page and the board in step: list what's on the
board (with when each was ticked off), add a to-do once per Tasks-page task, and tick one off
or bring it back. Every change is logged in agent_actions. Nothing here contacts anyone.
"""
from datetime import datetime, timedelta, timezone

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
