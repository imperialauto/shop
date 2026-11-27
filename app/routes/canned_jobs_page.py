"""Canned jobs: the owner's package list (/settings/canned-jobs) and the job page's Add a package button."""
import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import business_settings as store
from app import canned_jobs
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can change the packages.")
    return user


def _page(request, user, status=200, **extra):
    rows = [{**p, "text": canned_jobs.lines_text(p), "totals": canned_jobs.totals(p)} for p in canned_jobs.packages()]
    return templates.TemplateResponse("canned_jobs.html", {"request": request, "user": user, "packages": rows,
                                                           "types": canned_jobs.ITEM_TYPES, **extra}, status_code=status)


@router.get("/settings/canned-jobs", response_class=HTMLResponse)
def show(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    return _page(request, user, saved=request.query_params.get("saved"))


@router.post("/settings/canned-jobs")
async def update(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    form = await request.form()
    try:
        value = canned_jobs.clean(canned_jobs.DEFAULTS) if form.get("reset") == "1" else canned_jobs.parse_form(form)
    except canned_jobs.Invalid as exc:
        return _page(request, user, 422, error=str(exc))
    try:
        changed = store.save(db, canned_jobs.SETTING, json.dumps(value, sort_keys=True), user.username)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return _page(request, user, 503, error="Packages can't be saved until the settings table is set up.")
    store.clear_cache()
    return RedirectResponse(f"/settings/canned-jobs?saved={1 if changed else 0}", status_code=303)


@router.post("/ro/{ro_id}/add-package")
def add_package(request: Request, ro_id: int, package: str = Form(""), db: Session = Depends(get_db)):
    """Same rules as adding one line by hand: any signed-in staff, never onto a sent or signed invoice."""
    try:
        user = get_current_user(request, db)
    except HTTPException:
        user = None
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(404, "Job not found.")
    invoice = next((inv for inv in ro.invoices if inv.status != "void"), None)
    if invoice and (invoice.status != "draft" or invoice.customer_signed_at):
        return RedirectResponse(f"/ro/{ro_id}?line_error=invoiced#line-items", status_code=303)
    try:
        canned_jobs.add_to_job(db, ro, package)
    except canned_jobs.Invalid:
        return RedirectResponse(f"/ro/{ro_id}?package_error=1#line-items", status_code=303)
    if invoice:  # a draft invoice keeps up with the job, so the package is billed
        from app.routes.invoices import refresh_draft
        db.refresh(ro)
        refresh_draft(db, invoice, ro)
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}?package_added=1#line-items", status_code=303)
