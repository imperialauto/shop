"""Owner page: Security & Privacy Center. Every protection listed, each one editable or switchable."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import business_settings as store
from app import security_controls as controls
from app.auth import get_current_user
from app.database import get_db

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
PATH = "/settings/security-privacy"


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can change security and privacy settings.")
    return user


def _page(request: Request, db: Session, user, status: int = 200, **extra):
    try:
        history = (db.query(store.BusinessSettingChange)
                   .filter(store.BusinessSettingChange.key == controls.SETTING_KEY)
                   .order_by(store.BusinessSettingChange.id.desc()).limit(15).all())
    except SQLAlchemyError:
        db.rollback()
        history, extra = [], {"not_ready": True, **extra}
    return templates.TemplateResponse("security_center.html", {
        "request": request, "user": user, "groups": controls.page_rows(), "history": history, **extra,
    }, status_code=status, headers={"Cache-Control": "no-store"})


@router.get(PATH, response_class=HTMLResponse)
def show(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    return _page(request, db, user, saved=request.query_params.get("saved"))


@router.post(PATH)
async def update(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    form = await request.form()
    if form.get("reset") == "all":
        new_values = {}
    else:
        try:
            new_values = controls.parse(form)
        except controls.Invalid as exc:
            return _page(request, db, user, 422, error=str(exc))
    try:
        changed = controls.save(db, new_values, user.username)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return _page(request, db, user, 503, error="Settings can't be saved until the settings table is set up.")
    store.clear_cache()
    return RedirectResponse(f"{PATH}?saved={1 if changed else 0}", status_code=303)
