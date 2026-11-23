"""Shop TV board (/board) and the phone page that runs its to-do list (/board/manage)."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import shop_board
from app.auth import get_current_user
from app.database import get_db
from app.routes.imperial_tasks import enabled
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
REFRESH_SECONDS = 20


@router.get("/board", response_class=HTMLResponse)
def tv_board(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)  # not signed in: sign in, then back here
    return templates.TemplateResponse("shop_board.html", {
        "request": request, "user": user, "board": shop_board.snapshot(db, user),
        "refresh": REFRESH_SECONDS,
    })


@router.get("/board/panel", response_class=HTMLResponse)
def tv_board_panel(request: Request, db: Session = Depends(get_db)):
    """What the TV swaps in every few seconds. A signed-out screen says so instead of going blank."""
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return templates.TemplateResponse("shop_board_signed_out.html", {"request": request})
    return templates.TemplateResponse("shop_board_panel.html", {
        "request": request, "user": user, "board": shop_board.snapshot(db, user),
    })


@router.get("/board/manage", response_class=HTMLResponse)
def manage(request: Request, saved: str = "", error: str = "", db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    return templates.TemplateResponse("shop_board_manage.html", {
        "request": request, "user": user, "board": shop_board.snapshot(db, user),
        "goal": shop_board.daily_goal(), "saved": saved, "error": error[:200],
        "read_only": request.app.state.read_only,
    })


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
    if not shop_board.set_done(db, user, task_id, operation == "done"):
        raise HTTPException(404, "That to-do isn't on the shop board.")
    return _back(saved=operation)


@router.post("/board/goal")
def save_goal(request: Request, goal: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner sets the daily goal.")
    try:
        shop_board.save_goal(db, user, goal)
    except ValueError as exc:
        return _back(error=str(exc))
    return _back(saved="goal")
