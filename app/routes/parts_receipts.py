"""Parts receipts on a job: upload from the job page, or drop up to 15 on the Match a receipt page and let each
one's PO find the job. A store invoice whose PO names no job waits in a "not matched yet" list there until the
office puts it on one. Viewing, moving and removing are for the office. Nothing here texts anyone or changes a
price. Logic lives in app.parts_receipts and app.store_invoices.
"""
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import parts_receipts, po_numbers, store_invoices
from app.database import get_db
from app.models import RepairOrder
from app.parts_receipts import PartsReceipt
from app.routes.repair_orders import require_user
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
NOT_ON = "Parts receipts aren't switched on yet."
MAX_FILES = 15  # AutoZone Pro downloads 15 invoices at a time


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


def _picked(files: list) -> list[UploadFile]:
    """The files actually picked; the job page has two pickers (camera and files) and the unused one posts an empty part."""
    return [f for f in files or [] if not isinstance(f, str) and ((f.filename or "").strip() or (f.size or 0) > 0)]


def _pick(files: list) -> UploadFile | None:
    picked = _picked(files)
    return picked[0] if picked else None


def _where(row) -> str:
    return po_numbers.for_job(row.repair_order_id) if row.repair_order_id else "the not matched yet list"


def _invoice(db: Session, data: bytes, kind: str) -> dict | None:
    return store_invoices.parse(data, kind) if parts_receipts.lines_ready(db) else None


# ---------------------------------------------------------------- match a receipt to a job by its PO
# Registered before the job page's /ro/{ro_id} route, so /ro/receipts isn't read as a job number.

@router.get("/receipts", response_class=HTMLResponse)
def match_page(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db, office=True)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return _match_page(request, db, user, error=request.query_params.get("error", ""),
                       po=request.query_params.get("po", ""), saved=request.query_params.get("saved", ""))


def _match_page(request, db, user, *, error="", po="", saved="", results=None):
    rows = parts_receipts.unmatched(db)
    return templates.TemplateResponse("parts_receipt_match.html", {
        "request": request, "user": user, "ready": parts_receipts.ready(db), "lines_on": parts_receipts.lines_ready(db),
        "error": error, "po": po, "saved": saved, "results": results or [], "max_files": MAX_FILES,
        "unmatched": rows, "invoices": {r.id: parts_receipts.invoice_of(db, r) for r in rows},
    })


def _place(db: Session, user, data: bytes, kind: str, name: str, *, po: str = "", vendor: str = "", amount=None,
           note: str = "") -> dict:
    """Save one receipt on the job its PO names, or in the not matched yet list. Returns what happened."""
    invoice = _invoice(db, data, kind)
    number = (invoice or {}).get("invoice_number")
    result = {"name": name, "invoice_number": number, "job": None, "unmatched": False, "error": None,
              "total": (invoice or {}).get("total"), "printed_po": (invoice or {}).get("po")}
    earlier = parts_receipts.same_invoice(db, number)
    if earlier is not None:
        result["error"] = f"invoice {number} is already saved on {_where(earlier)}"
        return result
    typed = po_numbers.parse(po) if po.strip() else None
    if po.strip() and typed is None:
        result["error"] = "that doesn't look like a PO. Type it as it's printed, like IAC-PO-0021"
        return result
    on_receipt = parts_receipts.po_jobs_on(data, kind, name)
    printed = po_numbers.parse((invoice or {}).get("po"))
    if printed and printed not in on_receipt:
        on_receipt.insert(0, printed)
    if typed is None and len(on_receipt) > 1:
        result["error"] = ("this receipt shows more than one of our POs ("
                           + ", ".join(po_numbers.for_job(j) for j in on_receipt[:4]) + "). Type the one it's for")
        return result
    job_id = typed or (on_receipt[0] if on_receipt else None)
    if job_id is not None and db.get(RepairOrder, job_id) is None:
        if typed or invoice is None:
            result["error"] = f"there's no job for {po_numbers.for_job(job_id)}"
            return result
        job_id = None
    if job_id is None and invoice is None:
        result["error"] = ("no PO was found on this receipt. Type the PO printed on it, like IAC-PO-0021"
                           + (". Photos can't be read for a PO, so type it in" if kind != "application/pdf" else ""))
        return result
    db.add(parts_receipts.make(job_id, data, kind, name, user.username,
                               vendor=vendor or (invoice or {}).get("store"),
                               total=amount if amount is not None else (invoice or {}).get("total"),
                               po_text=(po.strip() or po_numbers.for_job(job_id)) if job_id else (invoice or {}).get("po"),
                               note=note, matched_by="po_on_receipt" if job_id and not typed else "po_typed",
                               invoice=invoice))
    db.commit()
    result["job"] = job_id
    result["unmatched"] = job_id is None
    other = next((j for j in on_receipt if j != job_id), None)
    result["other"] = po_numbers.for_job(other) if other and job_id else ""
    return result


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
    picked = _picked(files)
    if len(picked) > 1:
        if len(picked) > MAX_FILES:
            return again(f"Nothing was saved: pick up to {MAX_FILES} receipts at a time.")
        if po.strip():
            return again("Nothing was saved: type a PO only when adding one receipt. With several, each one's own PO is used.")
        results = []
        for upload in picked:
            try:
                data, kind, name = await _read(upload)
            except ValueError as exc:
                results.append({"name": upload.filename or "receipt", "error": str(exc), "job": None, "unmatched": False})
                continue
            results.append(_place(db, user, data, kind, name, note=note))
        return _match_page(request, db, user, results=results)
    try:
        data, kind, name = await _read(_pick(files))
        amount = parts_receipts.clean_total(total)
    except ValueError as exc:
        return again(f"Nothing was saved: {exc}.")
    result = _place(db, user, data, kind, name, po=po, vendor=vendor, amount=amount, note=note)
    if result["error"]:
        return again(f"Nothing was saved: {result['error']}.")
    if result["unmatched"]:
        return RedirectResponse("/ro/receipts?saved=unmatched#unmatched", status_code=303)
    return _back(result["job"], receipt="matched", po=po_numbers.for_job(result["job"]), other=result["other"])


# ---------------------------------------------------------------- the not matched yet list

def _waiting(db: Session, receipt_id: int) -> PartsReceipt:
    if not parts_receipts.lines_ready(db):
        raise HTTPException(404)
    row = db.query(PartsReceipt).filter(PartsReceipt.id == receipt_id, PartsReceipt.repair_order_id.is_(None)).first()
    if row is None:
        raise HTTPException(404)
    return row


@router.get("/receipts/unmatched/{receipt_id}")
def view_unmatched(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    if _user(request, db, office=True) is None:
        return RedirectResponse("/login", status_code=302)
    row = _waiting(db, receipt_id)
    return Response(row.data, media_type=row.content_type, headers={
        **PRIVATE, "Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename)}"})


@router.post("/receipts/unmatched/{receipt_id}/assign")
def assign_unmatched(request: Request, receipt_id: int, db: Session = Depends(get_db), po: str = Form("")):
    if _user(request, db, office=True) is None:
        return RedirectResponse("/login", status_code=302)
    row = _waiting(db, receipt_id)
    text = po.strip().lstrip("#")
    target = po_numbers.parse(text) or (int(text) if text.isdigit() and len(text) <= 9 else None)
    if not target or db.get(RepairOrder, target) is None:
        return RedirectResponse(f"/ro/receipts?error={quote('Not moved: no job has that PO or job number.')}#unmatched",
                                status_code=303)
    row.repair_order_id = target
    row.po_text = po_numbers.for_job(target)
    row.matched_by = "po_typed"
    db.commit()
    return _back(target, receipt="matched", po=po_numbers.for_job(target))


@router.post("/receipts/unmatched/{receipt_id}/delete")
def delete_unmatched(request: Request, receipt_id: int, db: Session = Depends(get_db)):
    if _user(request, db, office=True) is None:
        return RedirectResponse("/login", status_code=302)
    db.delete(_waiting(db, receipt_id))
    db.commit()
    return RedirectResponse("/ro/receipts?saved=removed#unmatched", status_code=303)


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
    invoice = _invoice(db, data, kind)
    earlier = parts_receipts.same_invoice(db, (invoice or {}).get("invoice_number"))
    if earlier is not None:
        return _back(ro_id, receipt_error=f"Nothing was saved: invoice {invoice['invoice_number']} is already saved on "
                     + ("this job" if earlier.repair_order_id == ro_id else _where(earlier)) + ".")
    db.add(parts_receipts.make(ro_id, data, kind, name, user.username,
                               vendor=vendor or (invoice or {}).get("store"),
                               total=amount if amount is not None else (invoice or {}).get("total"),
                               po_text=ro.po_number, note=note, matched_by="job_page", invoice=invoice))
    db.commit()
    found = parts_receipts.po_jobs_on(data, kind, name)
    printed = po_numbers.parse((invoice or {}).get("po"))
    if printed and printed not in found:
        found.insert(0, printed)
    other = next((j for j in found if j != ro_id), None)
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
