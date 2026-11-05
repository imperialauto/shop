"""One-time recovery for the existing Imperial Operations owner account.

Authorization is proof of access to the private Railway web-service variable
IMPERIAL_OWNER_SETUP_TOKEN; it is never stored in Git or displayed in responses.
Once claimed, the existing user is given role 'owner' and recovery is disabled.
"""
import hmac
import logging
import os
import re
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.auth import hash_password, login_user
from app.database import get_db
from app.models import User

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
log = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 32
MIN_PASSWORD_LENGTH = 14
MAX_PASSWORD_BYTES = 72


def setup_enabled() -> bool:
    return len(os.environ.get("IMPERIAL_OWNER_SETUP_TOKEN", "")) >= MIN_TOKEN_LENGTH


def existing_unclaimed_user(db: Session, lock: bool = False):
    # Do not create a second admin, overwrite multiple accounts, or reset a claimed owner.
    query = db.query(User).order_by(User.id.asc()).limit(2)
    users = (query.with_for_update() if lock else query).all()
    if len(users) != 1 or users[0].role == "owner":
        return None
    return users[0]


@router.get("/owner-setup", response_class=HTMLResponse)
def owner_setup_page(request: Request, db: Session = Depends(get_db)):
    if not setup_enabled():
        raise HTTPException(status_code=404, detail="Owner setup is not enabled.")
    if existing_unclaimed_user(db) is None:
        raise HTTPException(status_code=410, detail="One-time owner setup is unavailable.")
    csrf = secrets.token_urlsafe(32)
    request.session["owner_setup_csrf"] = csrf
    return templates.TemplateResponse("owner_setup.html", {"request": request, "csrf": csrf})


@router.post("/owner-setup", response_class=HTMLResponse)
def claim_owner_setup(
    request: Request,
    setup_token: str = Form(...),
    csrf: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    if not setup_enabled():
        raise HTTPException(status_code=404, detail="Owner setup is not enabled.")
    expected_csrf = request.session.get("owner_setup_csrf", "")
    if not expected_csrf or not hmac.compare_digest(expected_csrf, csrf):
        raise HTTPException(status_code=403, detail="Invalid form session. Reload and try again.")
    expected_token = os.environ.get("IMPERIAL_OWNER_SETUP_TOKEN", "")
    if not hmac.compare_digest(expected_token, setup_token):
        raise HTTPException(status_code=403, detail="Owner verification code is incorrect.")

    normalized = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", normalized):
        raise HTTPException(status_code=422, detail="Choose a username of 3 to 64 letters, digits, dots, dashes or underscores.")
    encoded_password = password.encode("utf-8")
    if not MIN_PASSWORD_LENGTH <= len(password) or len(encoded_password) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=422, detail="Use a passphrase of at least 14 characters (maximum 72 UTF-8 bytes).")
    if password != confirm_password:
        raise HTTPException(status_code=422, detail="Passwords do not match.")
    if password == setup_token or password == normalized:
        raise HTTPException(status_code=422, detail="Choose a different account password.")

    user = existing_unclaimed_user(db, lock=True)
    if user is None:
        raise HTTPException(status_code=410, detail="Owner access is already set up or requires an administrator.")
    user.username = normalized
    user.password_hash = hash_password(password)
    user.role = "owner"  # persistent one-time claim marker; existing account ID retained
    db.commit()
    request.session.pop("owner_setup_csrf", None)
    login_user(request, user)
    log.warning("Owner account securely claimed through Railway verification.")
    return RedirectResponse("/finance/", status_code=303)
