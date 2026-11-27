"""Owner-only estimate sheets: profit per line and per job, plus where to get each part.

Lives under /finance, which only the owner login can open (app/permissions.py OWNER_ONLY).
Nothing here sends anything to a customer.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.ro_status_policy import CLOSED_STATUSES
from app import owner_sheet
from app.auth import get_current_user
from app.database import get_db
from app.models import LineItem, RepairOrder
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


def _owner(request: Request, db: Session):
    try:
        user = get_current_user(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise
    if user.role != "owner":
        raise HTTPException(403, "Estimate profit sheets are owner-only.")
    return user


@router.get("/jobs", response_class=HTMLResponse)
def job_list(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    since = datetime.utcnow() - timedelta(days=30)
    ros = (db.query(RepairOrder)
           .filter((RepairOrder.status.notin_(CLOSED_STATUSES)) | (RepairOrder.updated_at >= since))
           .order_by(RepairOrder.id.desc()).limit(60).all())
    sheets = [owner_sheet.build(db, ro) for ro in ros]
    sheets = [s for s in sheets if s["main"].revenue or s["optional"]]
    open_approved = [s for s in sheets if s["approved"] and s["ro"].status not in CLOSED_STATUSES]
    summary = {
        "jobs": len(open_approved),
        "revenue": round(sum(s["main"].revenue for s in open_approved), 2),
        "profit": round(sum(s["main"].profit for s in open_approved), 2),
        "deposits_missing": sum(1 for s in open_approved if s["paid"] < s["deposit_due"]),
        "missing_costs": sum(s["main"].missing_costs for s in sheets),
    }
    return templates.TemplateResponse("finance/jobs.html", {
        "request": request, "user": user, "sheets": sheets, "summary": summary,
        "tax_rate": owner_sheet.tax_rate(), "margin_target": owner_sheet.MARGIN_TARGET,
    })


@router.get("/jobs/{ro_id}", response_class=HTMLResponse)
def job_sheet(request: Request, ro_id: int, saved: str = "", db: Session = Depends(get_db)):
    user = _owner(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404)
    sheet = owner_sheet.build(db, ro)
    return templates.TemplateResponse("finance/job_sheet.html", {
        "request": request, "user": user, "s": sheet, "saved": saved,
        "checked": datetime.now(timezone.utc),
    })


def _money(value: str) -> float | None:
    text = (value or "").replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        amount = float(text)
    except ValueError:
        raise HTTPException(400, "Cost has to be a dollar amount, like 47.69.")
    if amount < 0 or amount > 100000:
        raise HTTPException(400, "Cost has to be between $0 and $100,000.")
    return amount


@router.post("/jobs/{ro_id}/lines/{li_id}/source")
def save_part_source(request: Request, ro_id: int, li_id: int, db: Session = Depends(get_db),
                     cost: str = Form(""), part_number: str = Form(""), where: str = Form("")):
    user = _owner(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    ro = db.get(RepairOrder, ro_id)
    li = db.get(LineItem, li_id)
    if ro is None or li is None or li.repair_order_id != ro.id or li.item_type != "part":
        raise HTTPException(404)
    owner_sheet.save_source(db, ro, li, _money(cost), part_number, where)
    db.commit()
    return RedirectResponse(f"/finance/jobs/{ro.id}?saved={li.id}#line-{li.id}", status_code=303)
