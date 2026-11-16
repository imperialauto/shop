"""Per-line completion and photo/file evidence on a repair order.

Kept separate on purpose: marking a line done never changes its price, its customer
approval, the RO status, an invoice or a payment. Files are internal unless the office
marks one for the customer, and customers only ever see those on their private status page.
"""
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import LineItem, RepairOrder, RoAttachment
from app.routes.repair_orders import require_user

router = APIRouter()
public_router = APIRouter()

MAX_BYTES = 10 * 1024 * 1024
OFFICE = ("owner", "admin", "office")
STAFF = OFFICE + ("tech",)
PRIVATE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def sniff(head: bytes) -> str | None:
    """Content type from the file's own bytes, not its name or the browser's claim."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
        return "image/heic"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def _staff(request: Request, db: Session):
    user = require_user(request, db)
    if user is None:
        return None
    if user.role not in STAFF:
        raise HTTPException(403, "Shop staff only.")
    return user


def _ro(db: Session, ro_id: int) -> RepairOrder:
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404)
    return ro


def _line(db: Session, ro_id: int, li_id: int) -> LineItem:
    li = db.query(LineItem).filter(LineItem.id == li_id, LineItem.repair_order_id == ro_id).first()
    if li is None:
        raise HTTPException(404, "Line item not found.")
    return li


def _back(ro_id: int, **flag) -> RedirectResponse:
    query = "&".join(f"{k}={quote(str(v))}" for k, v in flag.items())
    return RedirectResponse(f"/ro/{ro_id}{'?' + query if query else ''}#line-items", status_code=303)


def progress(ro: RepairOrder) -> dict:
    """Done vs workable lines. Declined and still-pending lines don't count either way."""
    workable = [li for li in ro.line_items if li.workable]
    done = [li for li in workable if li.completed_at]
    return {"done": len(done), "total": len(workable), "all_done": bool(workable) and len(done) == len(workable)}


@router.post("/{ro_id}/lines/{li_id}/complete")
def complete_line(request: Request, ro_id: int, li_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    li = _line(db, ro_id, li_id)
    if not li.workable:
        raise HTTPException(409, "This line isn't approved by the customer, so it can't be marked done.")
    if li.completed_at is None:
        li.completed_at = datetime.now(timezone.utc)
        li.completed_by = user.username
        db.commit()
    return _back(ro_id, done=li.id)


@router.post("/{ro_id}/lines/{li_id}/reopen")
def reopen_line(request: Request, ro_id: int, li_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    li = _line(db, ro_id, li_id)
    li.completed_at = None
    li.completed_by = None
    db.commit()
    return _back(ro_id, reopened=li.id)


@router.post("/{ro_id}/attachments")
async def upload(request: Request, ro_id: int, db: Session = Depends(get_db),
                 files: list[UploadFile] = File(...), line_item_id: str = Form(""),
                 visibility: str = Form("internal")):
    user = _staff(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    _ro(db, ro_id)
    line_id = None
    if line_item_id.strip():
        try:
            line_id = _line(db, ro_id, int(line_item_id)).id
        except ValueError:
            raise HTTPException(422, "Pick a line from this job.")
    if visibility not in ("internal", "customer"):
        raise HTTPException(422, "Choose internal or customer-visible.")
    if visibility == "customer" and user.role not in OFFICE:
        visibility = "internal"  # techs' uploads start internal; the office decides what the customer sees
    rows, rejected = [], []
    for upload_ in files[:12]:
        data = await upload_.read(MAX_BYTES + 1)
        name = (upload_.filename or "file").rsplit("/", 1)[-1][:200] or "file"
        kind = sniff(data[:16])
        if not data:
            rejected.append(f"{name} (empty)")
        elif len(data) > MAX_BYTES:
            rejected.append(f"{name} (over 10 MB)")
        elif kind is None:
            rejected.append(f"{name} (not a photo or PDF)")
        else:
            rows.append(RoAttachment(repair_order_id=ro_id, line_item_id=line_id, filename=name, content_type=kind,
                                     size=len(data), data=data, visibility=visibility, uploaded_by=user.username))
    if rows:
        db.add_all(rows)
        db.commit()
    if rejected and not rows:
        return _back(ro_id, upload="failed", why="; ".join(rejected)[:300])
    return _back(ro_id, upload=len(rows), **({"why": "; ".join(rejected)[:300]} if rejected else {}))


def _attachment(db: Session, ro_id: int, att_id: int) -> RoAttachment:
    att = db.query(RoAttachment).filter(RoAttachment.id == att_id, RoAttachment.repair_order_id == ro_id).first()
    if att is None:
        raise HTTPException(404)
    return att


def _file_response(att: RoAttachment) -> Response:
    disposition = "inline" if att.content_type != "image/heic" else "attachment"
    return Response(att.data, media_type=att.content_type, headers={
        **PRIVATE, "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(att.filename)}"})


@router.get("/{ro_id}/attachments/{att_id}")
def download(request: Request, ro_id: int, att_id: int, db: Session = Depends(get_db)):
    if _staff(request, db) is None:
        return RedirectResponse("/login", status_code=302)
    return _file_response(_attachment(db, ro_id, att_id))


@router.post("/{ro_id}/attachments/{att_id}/visibility")
def set_visibility(request: Request, ro_id: int, att_id: int, db: Session = Depends(get_db),
                   visibility: str = Form(...)):
    user = _staff(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if user.role not in OFFICE:
        raise HTTPException(403, "Only the office decides what the customer sees.")
    if visibility not in ("internal", "customer"):
        raise HTTPException(422, "Choose internal or customer-visible.")
    _attachment(db, ro_id, att_id).visibility = visibility
    db.commit()
    return _back(ro_id, shared=att_id if visibility == "customer" else "")


@public_router.get("/status/{token}/files/{att_id}")
def customer_file(token: str, att_id: int, db: Session = Depends(get_db)):
    """A customer sees only files the office marked for them, only on their own job."""
    from app.main import _status_order
    ro = _status_order(db, token)
    att = db.query(RoAttachment).filter(RoAttachment.id == att_id, RoAttachment.repair_order_id == ro.id,
                                        RoAttachment.visibility == "customer").first()
    if att is None:
        raise HTTPException(404, "Not found")
    response = _file_response(att)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def attachments_for(db: Session, ro_id: int, customer_only: bool = False):
    """Metadata only; the bytes are never loaded for a page render."""
    from sqlalchemy.orm import load_only
    query = db.query(RoAttachment).options(load_only(
        RoAttachment.id, RoAttachment.line_item_id, RoAttachment.filename, RoAttachment.content_type,
        RoAttachment.size, RoAttachment.visibility, RoAttachment.uploaded_by, RoAttachment.created_at,
    )).filter(RoAttachment.repair_order_id == ro_id)
    if customer_only:
        query = query.filter(RoAttachment.visibility == "customer")
    return query.order_by(RoAttachment.created_at.asc(), RoAttachment.id.asc()).all()
