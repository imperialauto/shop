"""Shop TV board (/board) and the phone page that runs its to-do list (/board/manage)."""
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import shop_board
from app.auth import get_current_user
from app.database import get_db
from app.routes.imperial_tasks import enabled
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
REFRESH_SECONDS = 20
# Changes with every deploy. A TV left open across a deploy sees a new value in the panel and reloads itself.
BUILD = os.environ.get("RAILWAY_DEPLOYMENT_ID") or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or ""
TIMER_PRESETS = (5, 10, 15, 20, 30, 60)


@router.get("/board", response_class=HTMLResponse)
def tv_board(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)  # not signed in: sign in, then back here
    return templates.TemplateResponse("shop_board.html", {
        "request": request, "user": user, "board": shop_board.snapshot(db, user),
        "refresh": REFRESH_SECONDS, "build": BUILD,
    })


@router.get("/board/panel", response_class=HTMLResponse)
def tv_board_panel(request: Request, db: Session = Depends(get_db)):
    """What the TV swaps in every few seconds. A signed-out screen says so instead of going blank."""
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return templates.TemplateResponse("shop_board_signed_out.html", {"request": request})
    return templates.TemplateResponse("shop_board_panel.html", {
        "request": request, "user": user, "board": shop_board.snapshot(db, user), "build": BUILD,
    })


@router.get("/board/manage", response_class=HTMLResponse)
def manage(request: Request, saved: str = "", error: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse("shop_board_manage.html", {
        "request": request, "user": user, "board": shop_board.snapshot(db, user),
        "goal": shop_board.daily_goal(), "goal_mode": shop_board.goal_mode(), "presets": TIMER_PRESETS, "saved": saved, "error": error[:200], "lot": _lot(db),
        "read_only": request.app.state.read_only,
    })


def _lot(db):
    try:
        return shop_board.lot_list(db)
    except SQLAlchemyError:
        db.rollback()
        return None


def _back(**query):
    from urllib.parse import urlencode

    return RedirectResponse("/board/manage" + (f"?{urlencode(query)}" if query else "") + "#todo", status_code=303)


@router.post("/board/todos")
def add_todo(request: Request, title: str = Form(""), urgent: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not enabled():
        raise HTTPException(503, "Imperial task storage has not been activated.")
    try:
        shop_board.add_todo(db, user, title, urgent=bool(urgent))
    except ValueError as exc:
        return _back(error=str(exc))
    return _back(saved="added")


@router.post("/board/todos/{task_id}")
def change_todo(task_id: int, request: Request, operation: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not enabled():
        raise HTTPException(503, "Imperial task storage has not been activated.")
    if operation not in ("done", "undo"):
        raise HTTPException(422, "Unknown action.")
    try:
        found = shop_board.set_done(db, user, task_id, operation == "done")
    except ValueError as exc:
        return _back(error=str(exc))
    if not found:
        raise HTTPException(404, "That to-do isn't on the shop board.")
    return _back(saved=operation)


@router.post("/board/lot/{ro_id}")
def mark_lot(ro_id: int, request: Request, operation: str = Form(...), back: str = Form(""),
             db: Session = Depends(get_db)):
    """Here or gone: changes only which cars the TV lists, never the job itself.
    Pressed on Home or the job page, it goes back there (back=home or back=job)."""
    user = get_current_user(request, db)
    if request.app.state.read_only:
        raise HTTPException(503, "The dashboard is read-only right now.")
    if operation not in ("here", "gone"):
        raise HTTPException(422, "Unknown action.")
    if not shop_board.set_here(db, user, ro_id, operation == "here"):
        raise HTTPException(404, "That job isn't open.")
    where = {"home": f"/dashboard#job-{ro_id}", "job": f"/ro/{ro_id}"}.get(back, "/board/manage?saved=lot#lot")
    return RedirectResponse(where, status_code=303)


@router.post("/board/goal")
def save_goal(request: Request, goal: str = Form(""), mode: str = Form(""), db: Session = Depends(get_db)):
    """The daily goal in dollars and, when sent, which goal Home and the TV use: "fixed" or "bills"."""
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner sets the daily goal.")
    try:
        shop_board.save_goal(db, user, goal, mode=mode or None)
    except ValueError as exc:
        return _back(error=str(exc))
    return _back(saved="goal")


@router.post("/board/timers")
def start_timer(request: Request, label: str = Form(""), minutes: str = Form(""), custom: str = Form(""),
                db: Session = Depends(get_db)):
    """Start a countdown that shows big on the TV, like "Explorer pressure test" for 15 minutes."""
    user = get_current_user(request, db)
    if request.app.state.read_only:
        raise HTTPException(503, "The dashboard is read-only right now.")
    try:
        shop_board.start_timer(db, user, label, minutes.strip() or custom)
    except ValueError as exc:
        return RedirectResponse("/board/manage?" + _q(error=str(exc)) + "#timers", status_code=303)
    return RedirectResponse("/board/manage?saved=timer#timers", status_code=303)


@router.post("/board/timers/{timer_id}")
def change_timer(timer_id: str, request: Request, operation: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if request.app.state.read_only:
        raise HTTPException(503, "The dashboard is read-only right now.")
    if operation not in ("stop", "plus5"):
        raise HTTPException(422, "Unknown action.")
    if not shop_board.change_timer(db, user, timer_id[:16], operation):
        return RedirectResponse("/board/manage?" + _q(error="That timer already ended.") + "#timers", status_code=303)
    return RedirectResponse(f"/board/manage?saved={'stopped' if operation == 'stop' else 'plus5'}#timers", status_code=303)


def _q(**query):
    from urllib.parse import urlencode

    return urlencode(query)


@router.post("/board/clock/{ro_id}")
def job_clock(ro_id: int, request: Request, operation: str = Form(...), db: Session = Depends(get_db)):
    """Start or stop the work clock on a job, so the TV shows time worked against book time."""
    user = get_current_user(request, db)
    if request.app.state.read_only:
        raise HTTPException(503, "The dashboard is read-only right now.")
    if operation not in ("start", "stop"):
        raise HTTPException(422, "Unknown action.")
    if not shop_board.set_clock(db, user, ro_id, operation == "start"):
        raise HTTPException(404, "That job isn't open.")
    return RedirectResponse("/board/manage?saved=clock#lot", status_code=303)


@router.post("/ro/{ro_id}/clock")
def my_job_clock(ro_id: int, request: Request, operation: str = Form(...), db: Session = Depends(get_db)):
    """The job page's Start / Stop: clocks the signed-in worker (techs included) in or out on this job only."""
    user = get_current_user(request, db)
    if request.app.state.read_only:
        raise HTTPException(503, "The dashboard is read-only right now.")
    if operation not in ("start", "stop"):
        raise HTTPException(422, "Unknown action.")
    if not shop_board.set_clock(db, user, ro_id, operation == "start", only_mine=True):
        raise HTTPException(404, "That job isn't open.")
    return RedirectResponse(f"/ro/{ro_id}?clock={operation}#job-timer", status_code=303)
