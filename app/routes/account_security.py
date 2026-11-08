"""Second login step and authenticator setup."""
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import after_login, get_current_user, login_user, verify_password
from app.database import get_db
from app.models import User
from app.security import (PENDING_SECONDS, UserMfa, check_code, decrypt_secret, encrypt_secret, limiter,
                          new_recovery_codes, new_secret, setup_link, use_recovery_code)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
log = logging.getLogger("uvicorn.error")


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return "ip:" + (forwarded.split(",")[-1].strip() or (request.client.host if request.client else "unknown"))


def confirmed_mfa(db: Session, user_id: int) -> UserMfa | None:
    mfa = db.get(UserMfa, user_id)
    return mfa if mfa is not None and mfa.confirmed_at is not None else None


def _pending_user(request: Request, db: Session) -> User | None:
    pending = request.session.get("pending_mfa")
    if not pending or time.time() - pending.get("at", 0) > PENDING_SECONDS:
        request.session.pop("pending_mfa", None)
        return None
    return db.get(User, pending.get("user"))


@router.get("/login/verify", response_class=HTMLResponse)
def verify_page(request: Request, db: Session = Depends(get_db)):
    if _pending_user(request, db) is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("login_verify.html", {"request": request})


@router.post("/login/verify")
def verify_submit(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    user = _pending_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    keys = (f"mfa:{user.id}", client_key(request))
    wrong = {"request": request, "error": "That code didn't work. Check your authenticator and try again."}
    if limiter.locked(*keys):
        wrong["error"] = "Too many tries. Wait 15 minutes, then try again."
        return templates.TemplateResponse("login_verify.html", wrong, status_code=429)
    mfa = confirmed_mfa(db, user.id)
    secret = decrypt_secret(mfa.secret_encrypted) if mfa else None
    counter = check_code(secret, code, mfa.last_counter) if secret else None
    if counter is not None:
        mfa.last_counter = counter
    elif mfa is None or not use_recovery_code(mfa, code):
        limiter.record(*keys)
        return templates.TemplateResponse("login_verify.html", wrong, status_code=401)
    db.commit()
    limiter.clear(*keys)
    request.session.pop("pending_mfa", None)
    login_user(request, user)
    return RedirectResponse(after_login(request, "/ro/" if user.role == "tech" else "/dashboard"), status_code=303)


def _page(request: Request, user: User, db: Session, **extra):
    mfa = db.get(UserMfa, user.id)
    context = {"request": request, "user": user, "mfa": mfa, "enrolled": bool(mfa and mfa.confirmed_at), **extra}
    if mfa and not mfa.confirmed_at:
        secret = decrypt_secret(mfa.secret_encrypted)
        context.update(secret=secret, link=setup_link(secret, user.username))
    return templates.TemplateResponse("security.html", context)


@router.get("/settings/security", response_class=HTMLResponse)
def security_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
    return _page(request, user, db)


@router.post("/settings/security/start")
def security_start(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if confirmed_mfa(db, user.id) is None:
        mfa = db.get(UserMfa, user.id)
        if mfa is None:
            mfa = UserMfa(user_id=user.id, secret_encrypted="", recovery_hashes="[]")
            db.add(mfa)
        mfa.secret_encrypted = encrypt_secret(new_secret())
        db.commit()
    return RedirectResponse("/settings/security", status_code=303)


@router.post("/settings/security/confirm", response_class=HTMLResponse)
def security_confirm(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    mfa = db.get(UserMfa, user.id)
    if mfa is None or mfa.confirmed_at is not None:
        return RedirectResponse("/settings/security", status_code=303)
    counter = check_code(decrypt_secret(mfa.secret_encrypted) or "", code, 0)
    if counter is None:
        return _page(request, user, db, error="That code didn't match. Enter the current 6-digit code.")
    codes, hashes = new_recovery_codes()
    mfa.last_counter, mfa.recovery_hashes, mfa.confirmed_at = counter, hashes, datetime.now(timezone.utc)
    db.commit()
    log.info("mfa_enabled user=%s", user.id)
    return _page(request, user, db, recovery_codes=codes)


@router.post("/settings/security/disable", response_class=HTMLResponse)
def security_disable(request: Request, password: str = Form(...), code: str = Form(...), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    mfa = confirmed_mfa(db, user.id)
    if mfa is None:
        return RedirectResponse("/settings/security", status_code=303)
    counter = check_code(decrypt_secret(mfa.secret_encrypted) or "", code, mfa.last_counter)
    if counter is None or not verify_password(password, user.password_hash):
        return _page(request, user, db, error="Password or code was wrong. Two-step login stays on.")
    db.delete(mfa)
    db.commit()
    log.info("mfa_disabled user=%s", user.id)
    return RedirectResponse("/settings/security", status_code=303)
