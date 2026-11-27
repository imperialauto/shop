"""Parts receipts on a job: upload from the job page, or drop one on the Match a receipt page and let its PO
find the job. Viewing, moving and removing are for the office. Nothing here texts anyone or changes a price.
Logic lives in app.parts_receipts.
"""
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import parts_receipts, po_numbers
from app.database import get_db
from app.models import RepairOrder
from app.parts_receipts import PartsReceipt
from app.routes.repair_orders import require_user
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
NOT_ON = "Parts receipts aren't switched on yet."


def _user(request: Request, db: Session, *, office: bool):
    user = require_user(request, db)
    if user is None:
        return None
    allowed = parts_receipts.can_view(user.role) if office else parts_receipts.can_upload(user.role)
    if not allowed:
        raise HTTPException(403, "The office handles parts receipts.")
    return user


def _back(ro_id: int, **flag) -> RedirectResponse:
    query = "&".join(f"{k}={quote(str(v))}" for k, v in flag.items() if v not in (None, ""))
    return RedirectResponse(f"/ro/{ro_id}{'?' + query if query else ''}#receipts", status_code=303)


async def _read(upload: UploadFile | str | None):
    """(bytes, content type, name) of one picked file, or raises ValueError with what's wrong."""
    if upload is None or isinstance(upload, str) or not ((upload.filename or "").strip() or (upload.size or 0) > 0):
        raise ValueError("no receipt was picked")
    data = await upload.read(parts_receipts.MAX_BYTES + 1)
    name = (upload.filename or "receipt").rsplit("/", 1)[-1][:200] or "receipt"
    if not data:
        raise ValueError(f"{name} is empty")
    if len(data) > parts_receipts.MAX_BYTES:
        raise ValueError(f"{name} is over 10 MB")
    kind = parts_receipts.sniff(data[:16])
    if kind is None:
        raise ValueError(f"{name} isn't a photo or PDF")
    return data, kind, name


def _pick(files: list) -> UploadFile | None:
    """The job page has two pickers (camera and files); the unused one posts an empty part."""
    for f in files or []:
        if not isinstance(f, str) and ((f.filename or "").strip() or (f.size or 0) > 0):
            return f
    return None


# ---------------------------------------------------------------- match a receipt to a job by its PO
# Registered before the job page's /ro/{ro_id} route, so /ro/receipts isn't read as a job number.

@router.get("/receipts", response_class=HTMLResponse)
def match_page(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("parts_receipt_match.html", {
        "request": request, "user": user, "ready": parts_receipts.ready(db),
        "error": request.query_params.get("error", ""), "po": request.query_params.get("po", ""),
    })


@router.post("/receipts")
async def match_upload(request: Request, db: Session = Depends(get_db), files: list[UploadFile | str] = File(...),
                       po: str = Form(""), vendor: str = Form(""), total: str = Form(""), note: str = Form("")):
    user = _user(request, db, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)

    def again(error: str):
        return RedirectResponse(f"/ro/receipts?error={quote(error[:300])}&po={quote(po[:64])}", status_code=303)

    if not parts_receipts.ready(db):
        return again(NOT_ON)
    try:
        data, kind, name = await _read(_pick(files))
        amount = parts_receipts.clean_total(total)
    except ValueError as exc:
        return again(f"Nothing was saved: {exc}.")
    typed = po_numbers.parse(po) if po.strip() else None
    if po.strip() and typed is None:
        return again("Nothing was saved: that doesn't look like a PO. Type it as it's printed, like IAC-PO-0021.")
    on_receipt = parts_receipts.po_jobs_on(data, kind, name)
    if typed is None and len(on_receipt) > 1:
        return again("Nothing was saved: this receipt shows more than one of our POs ("
                     + ", ".join(po_numbers.for_job(j) for j in on_receipt[:4]) + "). Type the one it's for.")
    job_id = typed or (on_receipt[0] if on_receipt else None)
    if job_id is None:
        return again("Nothing was saved: no PO was found on this receipt. Type the PO printed on it, like IAC-PO-0021."
                     + (" Photos can't be read for a PO, so type it in." if kind != "application/pdf" else ""))
    if db.get(RepairOrder, job_id) is None:
        return again(f"Nothing was saved: there's no job for {po_numbers.for_job(job_id)}.")
    db.add(parts_receipts.make(job_id, data, kind, name, user.username, vendor=vendor, total=amount,
                               po_text=po.strip() or po_numbers.for_job(job_id), note=note,
                               matched_by="po_typed" if typed else "po_on_receipt"))
    db.commit()
    other = next((j for j in on_receipt if j != job_id), None)
    return _back(job_id, receipt="matched", po=po_numbers.for_job(job_id),
                 other=po_numbers.for_job(other) if other else "")


# ---------------------------------------------------------------- on one job

@router.post("/{ro_id}/receipts")
async def upload(request: Request, ro_id: int, db: Session = Depends(get_db), files: list[UploadFile | str] = File(...),
                 vendor: str = Form(""), total: str = Form(""), note: str = Form("")):
    user = _user(request, db, office=False)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404)
    if not parts_receipts.ready(db):
        return _back(ro_id, receipt_error=NOT_ON)
    try:
        data, kind, name = await _read(_pick(files))
        amount = parts_receipts.clean_total(total)
    except ValueError as exc:
        return _back(ro_id, receipt_error=f"Nothing was saved: {exc}.")
    db.add(parts_receipts.make(ro_id, data, kind, name, user.username, vendor=vendor, total=amount,
                               po_text=ro.po_number, note=note, matched_by="job_page"))
    db.commit()
    other = next((j for j in parts_receipts.po_jobs_on(data, kind, name) if j != ro_id), None)
    return _back(ro_id, receipt="saved", other=po_numbers.for_job(other) if other else "")


def _receipt(db: Session, ro_id: int, receipt_id: int) -> PartsReceipt:
    if not parts_receipts.ready(db):
        raise HTTPException(404)
    row = db.query(PartsReceipt).filter(PartsReceipt.id == receipt_id, PartsReceipt.repair_order_id == ro_id).first()
    if row is None:
        raise HTTPException(404)
    return row


@router.get("/{ro_id}/receipts/{receipt_id}")
def view(request: Request, ro_id: int, receipt_id: int, db: Session = Depends(get_db)):
    if _user(request, db, office=True) is None:
        return RedirectResponse("/login", status_code=302)
    row = _receipt(db, ro_id, receipt_id)
    disposition = "inline" if row.content_type != "image/heic" else "attachment"
    return Response(row.data, media_type=row.content_type, headers={
        **PRIVATE, "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(row.filename)}"})


@router.post("/{ro_id}/receipts/{receipt_id}/move")
def move(request: Request, ro_id: int, receipt_id: int, db: Session = Depends(get_db), po: str = Form("")):
    """Put a receipt saved on the wrong job onto the job its PO (or job number) names."""
    if _user(request, db, office=True) is None:
        return RedirectResponse("/login", status_code=302)
    row = _receipt(db, ro_id, receipt_id)
    text = po.strip().lstrip("#")
    target = po_numbers.parse(text) or (int(text) if text.isdigit() and len(text) <= 9 else None)
    if not target or db.get(RepairOrder, target) is None:
        return _back(ro_id, receipt_error="Not moved: no job has that PO or job number.")
    if target == ro_id:
        return _back(ro_id, receipt="saved")
    row.repair_order_id = target
    row.po_text = po_numbers.for_job(target)
    db.commit()
    return _back(target, receipt="moved", came_from=po_numbers.for_job(ro_id))


@router.post("/{ro_id}/receipts/{receipt_id}/delete")
def delete(request: Request, ro_id: int, receipt_id: int, db: Session = Depends(get_db)):
    if _user(request, db, office=True) is None:
        return RedirectResponse("/login", status_code=302)
    db.delete(_receipt(db, ro_id, receipt_id))
    db.commit()
    return _back(ro_id, receipt="removed")
