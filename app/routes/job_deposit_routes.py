"""Record a deposit on a job from its page (owner, admin and office). Nothing is texted to anyone."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import job_deposits
from app.database import get_db
from app.models import RepairOrder
from app.routes.invoices import require_user

router = APIRouter()
RECORDERS = ("owner", "admin", "office")


def _job(db, ro_id):
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)
    return ro


@router.post("/{ro_id}/deposits")
def add_deposit(request: Request, ro_id: int, db: Session = Depends(get_db), amount: float = Form(...),
                method: str = Form("zelle"), reference: str = Form(""), paid_on: str = Form("")):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in RECORDERS:
        raise HTTPException(status_code=403)
    ro = _job(db, ro_id)
    dep, error = job_deposits.record(db, ro, amount, method, reference, paid_on, by=user.username)
    if error:
        return RedirectResponse(f"/ro/{ro_id}?deposit_error={quote(error)}#deposits", status_code=303)
    return RedirectResponse(f"/ro/{ro_id}?deposit=recorded#deposits", status_code=303)


@router.post("/{ro_id}/deposits/{dep_id}/remove")
def remove_deposit(request: Request, ro_id: int, dep_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "owner":
        raise HTTPException(status_code=403)
    ro = _job(db, ro_id)
    error = job_deposits.remove(db, ro, dep_id, by=user.username)
    if error:
        return RedirectResponse(f"/ro/{ro_id}?deposit_error={quote(error)}#deposits", status_code=303)
    return RedirectResponse(f"/ro/{ro_id}?deposit=removed#deposits", status_code=303)
