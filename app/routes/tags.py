"""Add, remove and search tags on jobs and customers."""
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import tags
from app.auth import get_current_user
from app.database import get_db
from app.models import Customer, RepairOrder
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


def _user(request: Request, db: Session):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def _back(next_page: str, kind: str, target_id: int) -> str:
    if next_page.startswith("/") and not next_page.startswith("//") and "\\" not in next_page:
        return next_page
    return f"/ro/{target_id}" if kind == "job" else f"/customers/{target_id}"


def _exists(db: Session, kind: str, target_id: int) -> bool:
    model = RepairOrder if kind == "job" else Customer
    return db.get(model, target_id) is not None


@router.post("/add")
def add_tag(request: Request, kind: str = Form(...), target_id: int = Form(...), name: str = Form(""),
            next_page: str = Form("", alias="next"), db: Session = Depends(get_db)):
    user = _user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if kind not in tags.KINDS or not _exists(db, kind, target_id):
        raise HTTPException(404)
    back = _back(next_page, kind, target_id)
    problem = None
    for piece in name.split(","):
        if piece.strip():
            problem = tags.add(db, kind, target_id, piece, user.username) or problem
    if problem is None and not name.strip():
        problem = "Type a tag first: letters, numbers, spaces or dashes."
    if problem:
        back += ("&" if "?" in back else "?") + "tag_error=" + quote(problem)
    return RedirectResponse(back, status_code=303)


@router.post("/remove")
def remove_tag(request: Request, kind: str = Form(...), target_id: int = Form(...), name: str = Form(...),
               next_page: str = Form("", alias="next"), db: Session = Depends(get_db)):
    user = _user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if kind not in tags.KINDS:
        raise HTTPException(404)
    tags.remove(db, kind, target_id, name)
    return RedirectResponse(_back(next_page, kind, target_id), status_code=303)


@router.get("/", response_class=HTMLResponse)
def search_tags(request: Request, q: str = "", db: Session = Depends(get_db)):
    """Every tag in use, and the jobs and customers that carry the one searched for."""
    user = _user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    jobs, customers = [], []
    if q.strip():
        job_ids = tags.matching(db, "job", q)
        customer_ids = tags.matching(db, "customer", q)
        if job_ids:
            jobs = db.query(RepairOrder).filter(RepairOrder.id.in_(job_ids)).order_by(RepairOrder.created_at.desc()).limit(100).all()
        if customer_ids:
            customers = db.query(Customer).filter(Customer.id.in_(customer_ids)).order_by(Customer.last_name).limit(100).all()
    return templates.TemplateResponse("tags.html", {
        "request": request, "user": user, "q": q, "all_tags": tags.known(db), "ready": tags.ready(db),
        "jobs": jobs, "customers": customers,
        "job_tags": tags.names_for(db, "job", [j.id for j in jobs]),
        "customer_tags": tags.names_for(db, "customer", [c.id for c in customers]),
    })
