"""Event intake and owner-only customer journey view."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app import journey
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.public_site import preview_mode

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
PRIVATE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}


@router.post("/journey/event")
async def collect_event(request: Request, db: Session = Depends(get_db)):
    """Only fixed enum labels; no form fields, paths, URLs, device IDs or free text."""
    if not journey.ready(db) or request.app.state.read_only:
        return Response(status_code=204, headers=PRIVATE)
    if "application/json" not in request.headers.get("content-type", "").lower():
        raise HTTPException(status_code=415)
    try:
        if int(request.headers.get("content-length", "0") or "0") > 1024:
            raise HTTPException(status_code=413)
    except ValueError:
        raise HTTPException(status_code=413)
    if len(await request.body()) > 1024:
        raise HTTPException(status_code=413)
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400)
    sid, page, event, step = (
        payload.get("sid"), payload.get("page"), payload.get("event"), payload.get("step")
    )
    if (not journey.valid_sid(sid) or not all(isinstance(v, str) for v in (page, event, step))
            or page not in journey.PAGES or event not in journey.EVENTS or step not in journey.STEPS):
        raise HTTPException(status_code=400, detail="Invalid tracking event")
    ro_id = None
    if page in journey.SECURE_PAGES:
        ro_id = journey.context_ro_id(payload.get("context"))
        if not ro_id or not db.get(RepairOrder, ro_id):
            raise HTTPException(status_code=400, detail="Invalid page context")
    saved = journey.record(db, sid, page, event, step, ro_id=ro_id,
                           is_preview=preview_mode())
    if saved:
        db.commit()
    return Response(status_code=204, headers=PRIVATE)


@router.get("/journey/", response_class=HTMLResponse)
def owner_journey(request: Request, days: int = 7, environment: str = "live",
                  db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user.role not in ("owner", "admin"):
        raise HTTPException(status_code=403)
    if days not in (7, 30) or environment not in ("live", "preview"):
        raise HTTPException(status_code=422)
    active = journey.ready(db)
    metrics = journey.report(db, days=days, show_preview=environment == "preview") if active else None
    return templates.TemplateResponse("journey.html", {
        "request": request, "user": user, "metrics": metrics, "tracking_enabled": active,
        "days": days, "environment": environment,
        "migration_needed": journey.enabled() and not active,
    }, headers=PRIVATE)
