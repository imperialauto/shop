"""Owner Alert Center: what was sent, and the rules for what gets sent."""
import re
from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import alert_rules, business_settings
from app.appointments import PHOENIX
from app.auth import get_current_user
from app.database import get_db
from app.dispatch_model import DispatchItem, DispatchOutbound

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Quo API success is not a carrier or handset delivery receipt, and uncertain
# customer sends are intentionally never retried automatically.
STATUS = {"sent": "Sent via Quo (delivery unconfirmed)", "failed": "Failed — review needed",
          "reserved": "Send unconfirmed", "skipped": "Not sent", "queued": "Waiting to send"}


def recipient_for(kind: str) -> str:
    """Separate owner alerts from customer replies in the same outbound log."""
    return "Customer" if kind in ("customer", "dashboard_reply") else "Owner"


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can change alerts.")
    return user


def page(request, db, user, status=200, **extra):
    rows = db.query(DispatchOutbound).order_by(DispatchOutbound.id.desc()).limit(60).all()
    items = {i.id: i for i in db.query(DispatchItem).filter(DispatchItem.id.in_([r.item_id for r in rows if r.item_id])).all()}
    history = [{"when": (r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)).astimezone(PHOENIX),
                "what": alert_rules.LABELS.get(r.kind, r.kind.replace("_", " ")), "item": r.item_id,
                "about": " ".join((items[r.item_id].summary if r.item_id in items else "").split())[:70],
                "status": STATUS.get(r.status, r.status), "failed": r.status == "failed",
                "recipient": recipient_for(r.kind)} for r in rows]
    return templates.TemplateResponse("alert_center.html", {"request": request, "user": user, "rules": alert_rules.rules(),
                                      "optional": alert_rules.OPTIONAL, "history": history, **extra}, status_code=status)


@router.get("/alerts", response_class=HTMLResponse)
def show(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    return page(request, db, user, saved=request.query_params.get("saved") == "1")


@router.post("/alerts")
async def update(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    form = await request.form()
    start, end = str(form.get("quiet_start", "")).strip(), str(form.get("quiet_end", "")).strip()
    if (start or end) and not (CLOCK.match(start) and CLOCK.match(end) and start != end):
        return page(request, db, user, 422, error="Quiet hours need a start and an end time, or leave both blank.")
    off = [kind for kind in alert_rules.OPTIONAL if form.get(f"on_{kind}") != "on"]
    try:
        business_settings.save(db, "alert_rules", {"quiet_start": start, "quiet_end": end, "off": off}, user.username)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return page(request, db, user, 503, error="Alert rules can't be saved right now.")
    business_settings.clear_cache()
    return RedirectResponse("/alerts?saved=1", status_code=303)
