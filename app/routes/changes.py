"""Owner-only Changes page (/changes): what's being worked on in the dashboard and website, and his yes on each change.

Approving only records the owner's yes on that exact version. Nothing here merges, deploys,
turns anything on or contacts anyone. See app/change_review.py.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import change_review
from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.routes import dev_work
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def require_owner(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "The Changes page is owner-only.")
    return user


def _fix_list_count(db: Session) -> int | None:
    try:
        from app.routes import imperial_tasks
        return len(imperial_tasks.problem_reports(db))
    except Exception:  # tasks table missing or turned off: leave the line out
        return None


@router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, refresh: bool = False, saved: str = "", db: Session = Depends(get_db),
                 user: User = Depends(require_owner)):
    data = change_review.fetch(force=refresh)
    sections = change_review.grouped(data["changes"], change_review.decisions())
    waiting = sum(len(s["changes"]) for s in sections if s["key"] in ("ready", "changed"))
    return templates.TemplateResponse("changes.html", {
        "request": request, "user": user, "data": data, "sections": sections, "waiting": waiting,
        "decision_names": change_review.DECISIONS, "saved": saved,
        "dev_work": dev_work.dashboard_snapshot(db, user.role), "fix_list_count": _fix_list_count(db),
    })


@router.post("/changes/{number}")
def decide(number: int, decision: str = Form(...), sha: str = Form(""), note: str = Form(""),
           db: Session = Depends(get_db), user: User = Depends(require_owner)):
    try:
        change_review.record(db, number, sha.strip().lower(), decision, note, f"owner:{user.id}")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    db.commit()
    return RedirectResponse(f"/changes?saved={number}#change-{number}", status_code=303)
