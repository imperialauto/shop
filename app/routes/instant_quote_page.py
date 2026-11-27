"""The owner's price book for instant website prices (/settings/instant-quotes)."""
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import business_settings as store
from app import instant_quote, pricing
from app.auth import get_current_user
from app.database import get_db
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can change the price book.")
    return user


def _page(request, user, status=200, **extra):
    rows = [{**j, "vehicles_text": instant_quote.vehicles_text(j), "words_text": ", ".join(j["parts_words"])}
            for j in instant_quote.jobs()]
    return templates.TemplateResponse("instant_quotes.html", {
        "request": request, "user": user, "jobs": rows, "rates": pricing.rates(), "live": instant_quote.enabled(),
        "supplies_percent": instant_quote.SUPPLIES_PERCENT, "supplies_cap": instant_quote.SUPPLIES_CAP, **extra},
        status_code=status)


@router.get("/settings/instant-quotes", response_class=HTMLResponse)
def show(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    return _page(request, user, saved=request.query_params.get("saved"))


@router.post("/settings/instant-quotes")
async def update(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    form = await request.form()
    try:
        value = (instant_quote.clean(instant_quote.DEFAULTS) if form.get("reset") == "1"
                 else instant_quote.parse_form(form))
    except instant_quote.Invalid as exc:
        return _page(request, user, 422, error=str(exc))
    try:
        changed = store.save(db, instant_quote.SETTING, json.dumps(value, sort_keys=True), user.username)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return _page(request, user, 503, error="The price book can't be saved until the settings table is set up.")
    store.clear_cache()
    return RedirectResponse(f"/settings/instant-quotes?saved={1 if changed else 0}", status_code=303)
