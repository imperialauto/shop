"""Job page Parts card actions: add, edit, ordered, arrived, returned, and the offered parts hold.

Office, owner and admin can do everything; technicians can only mark a part Arrived.
Nothing here texts a customer. Logic lives in app.parts_tracking.
"""
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import parts_tracking
from app.database import get_db
from app.models import PartsOrder, RepairOrder
from app.routes import repair_orders

router = APIRouter()


def _back(ro_id: int, note: str = "", error: str = "") -> RedirectResponse:
    query = f"?parts={quote(note)}" if note else (f"?parts_error={quote(error)}" if error else "")
    return RedirectResponse(f"/ro/{ro_id}{query}#parts", status_code=302)


def _load(request: Request, db: Session, ro_id: int, part_id: int | None, *, office: bool):
    user = repair_orders.require_user(request, db)
    if not user:
        return None, None, None
    allowed = parts_tracking.can_edit(user.role) if office else parts_tracking.can_mark_arrived(user.role)
    if not allowed:
        raise HTTPException(status_code=403, detail="The office handles parts orders.")
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(status_code=404)
    part = None
    if part_id is not None:
        part = db.get(PartsOrder, part_id)
        if part is None or part.repair_order_id != ro.id:
            raise HTTPException(status_code=404)
    return user, ro, part


@router.post("/{ro_id}/parts/add")
def add_part(request: Request, ro_id: int, db: Session = Depends(get_db),
             description: str = Form(""), part_number: str = Form(""), supplier: str = Form(""),
             quantity: str = Form("1"), cost: str = Form(""), eta: str = Form("")):
    user, ro, _ = _load(request, db, ro_id, None, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    try:
        parts_tracking.add_part(db, ro, description=description, part_number=part_number, supplier=supplier,
                                quantity=quantity, cost=cost, eta=eta)
    except ValueError as exc:
        db.rollback()
        return _back(ro_id, error=str(exc))
    db.commit()
    return _back(ro_id, "added")


@router.post("/{ro_id}/parts/{part_id}/edit")
def edit_part(request: Request, ro_id: int, part_id: int, db: Session = Depends(get_db),
              description: str = Form(""), part_number: str = Form(""), supplier: str = Form(""),
              quantity: str = Form("1"), cost: str | None = Form(None), eta: str = Form("")):
    user, ro, part = _load(request, db, ro_id, part_id, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    try:
        parts_tracking.edit_part(db, part, description=description, part_number=part_number, supplier=supplier,
                                 quantity=quantity, cost=cost, eta=eta)
    except ValueError as exc:
        db.rollback()
        return _back(ro_id, error=str(exc))
    db.commit()
    return _back(ro_id, "saved")


@router.post("/{ro_id}/parts/{part_id}/arrived")
def part_arrived(request: Request, ro_id: int, part_id: int, db: Session = Depends(get_db)):
    user, ro, part = _load(request, db, ro_id, part_id, office=False)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if part.status not in ("ordered", "received"):
        return _back(ro_id, error="Mark the part ordered first.")
    moved = parts_tracking.mark_arrived(db, ro, part, user.username)
    db.commit()
    return _back(ro_id, "moved" if moved else "arrived")


@router.post("/{ro_id}/parts/{part_id}/ordered")
def part_ordered(request: Request, ro_id: int, part_id: int, db: Session = Depends(get_db)):
    user, ro, part = _load(request, db, ro_id, part_id, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    parts_tracking.mark_ordered(db, part)
    db.commit()
    return _back(ro_id, "ordered")


@router.post("/{ro_id}/parts/{part_id}/returned")
def part_returned(request: Request, ro_id: int, part_id: int, db: Session = Depends(get_db)):
    user, ro, part = _load(request, db, ro_id, part_id, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    parts_tracking.mark_returned(db, part)
    moved = parts_tracking.advance_if_ready(db, ro, user.username)
    db.commit()
    return _back(ro_id, "moved" if moved else "returned")


@router.post("/{ro_id}/parts/hold")
def hold_for_parts(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user, ro, _ = _load(request, db, ro_id, None, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    held = parts_tracking.hold_for_parts(db, ro, user.username)
    db.commit()
    return _back(ro_id, "held") if held else _back(ro_id, error="The job can't wait on parts right now.")
