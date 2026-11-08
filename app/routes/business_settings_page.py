"""Owner page for hours, booking services, prices and mobile ZIP fees."""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import business_settings as store
from app.auth import get_current_user
from app.database import get_db
from app.public_site import BOOKABLE_SERVICES

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class Invalid(ValueError):
    pass


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can change business settings.")
    return user


def _json(key: str, default):
    try:
        value = json.loads(store.raw(key) or "null")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def current() -> dict:
    hours = _json("shop_hours", {})
    services = _json("booking_services", {})
    zones = _json("mobile_zones", {})
    closed = {int(d) for d in (store.raw("booking_closed_days") or "").split(",") if d.strip().isdigit()}
    return {
        "hours": [{"day": name, "open": str(i) in hours, "opens": (hours.get(str(i)) or ["", ""])[0],
                   "closes": (hours.get(str(i)) or ["", ""])[1], "online": i not in closed} for i, name in enumerate(DAYS)],
        "services": [{"slug": s.slug, "name": s.name, "price": (services.get(s.slug) or {}).get("price"),
                      "price_type": (services.get(s.slug) or {}).get("price_type", "exact"),
                      "minutes": (services.get(s.slug) or {}).get("minutes", s.minutes),
                      "mobile": (services.get(s.slug) or {}).get("mobile") is True} for s in BOOKABLE_SERVICES],
        "zones": "\n".join(f"{z}, {v.get('fee', '')}, {v.get('travel_minutes', '')}" for z, v in sorted(zones.items()) if isinstance(v, dict)),
        "buffer": store.raw("shop_buffer_minutes") or "60",
        "lead": store.raw("booking_lead_minutes") or "120",
    }


def _number(text: str, label: str, low: float, high: float, whole: bool = True):
    try:
        value = int(text) if whole else round(float(text), 2)
    except ValueError:
        raise Invalid(f"{label}: enter a number.") from None
    if not low <= value <= high:
        raise Invalid(f"{label}: must be between {low:g} and {high:g}.")
    return value


def parse(form) -> dict:
    hours, closed = {}, []
    for i, name in enumerate(DAYS):
        if form.get(f"online_{i}") != "on":
            closed.append(str(i))
        if form.get(f"open_{i}") != "on":
            continue
        opens, closes = form.get(f"opens_{i}", "").strip(), form.get(f"closes_{i}", "").strip()
        if not (TIME.match(opens) and TIME.match(closes)) or opens >= closes:
            raise Invalid(f"{name}: enter an opening time before the closing time.")
        hours[str(i)] = [opens, closes]
    services = {}
    for s in BOOKABLE_SERVICES:
        price_text = form.get(f"price_{s.slug}", "").strip().lstrip("$").replace(",", "")
        entry = {"minutes": _number(form.get(f"minutes_{s.slug}", "").strip(), f"{s.name} length", 15, 480),
                 "price_type": "starting" if form.get(f"type_{s.slug}") == "starting" else "exact",
                 "mobile": form.get(f"mobile_{s.slug}") == "on"}
        if price_text:
            entry["price"] = _number(price_text, f"{s.name} price", 0, 100000, whole=False)
        services[s.slug] = entry
    zones = {}
    for number, line in enumerate(form.get("zones", "").splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip().lstrip("$") for p in line.split(",")]
        if len(parts) != 3 or not re.fullmatch(r"\d{5}", parts[0]):
            raise Invalid(f"Mobile ZIP line {number}: use ZIP, fee, travel minutes. Example: 85307, 25, 20")
        zones[parts[0]] = {"fee": _number(parts[1], f"ZIP {parts[0]} fee", 0, 1000, whole=False),
                           "travel_minutes": _number(parts[2], f"ZIP {parts[0]} travel", 0, 240)}
    return {"shop_hours": hours, "booking_closed_days": ",".join(closed), "booking_services": services, "mobile_zones": zones,
            "shop_buffer_minutes": str(_number(form.get("buffer", "").strip(), "Buffer after shop jobs", 0, 240)),
            "booking_lead_minutes": str(_number(form.get("lead", "").strip(), "Earliest online booking", 0, 10080))}


def page(request: Request, db: Session, user, status: int = 200, **extra):
    history = db.query(store.BusinessSettingChange).order_by(store.BusinessSettingChange.id.desc()).limit(15).all()
    return templates.TemplateResponse("business_settings.html", {"request": request, "user": user, "settings": current(),
                                      "history": history, **extra}, status_code=status)


@router.get("/settings/business", response_class=HTMLResponse)
def show(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    return page(request, db, user, saved=request.query_params.get("saved") == "1")


@router.post("/settings/business")
async def update(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    try:
        values = parse(await request.form())
    except Invalid as exc:
        return page(request, db, user, 422, error=str(exc))
    changed = [key for key, value in values.items() if store.save(db, key, value, user.username)]
    db.commit()
    store.clear_cache()
    return RedirectResponse(f"/settings/business?saved={1 if changed else 0}", status_code=303)
