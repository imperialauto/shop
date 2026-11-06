"""Owner-managed staff logins. Logins are turned off, never deleted, so history stays."""
import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user, hash_password
from app.database import get_db
from app.models import User
from app.permissions import ROLES
from app.security import UserMfa

router = APIRouter()
log = logging.getLogger("uvicorn.error")
MIN_PASSWORD = 12
MAX_PASSWORD_BYTES = 72
USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}")
MESSAGES = {
    "created": "Login created. Share the temporary password in person. They'll set their own at first sign-in.",
    "reset": "Password reset. Two-step login was cleared, and they'll set a new password at next sign-in.",
    "off": "Login turned off.",
    "on": "Login turned back on.",
}
ERRORS = {
    "username": "Use 3 or more lowercase letters or numbers for the username.",
    "taken": "That username is already in use.",
    "password": f"Passwords need at least {MIN_PASSWORD} characters.",
    "role": "Choose a role.",
    "missing": "That login could not be found.",
}


def password_ok(password: str) -> bool:
    return len(password) >= MIN_PASSWORD and len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES


def _back(key: str, error: bool = False):
    return RedirectResponse(f"/settings?{'user_error' if error else 'user_saved'}={key}", status_code=303)


def _owner(request: Request, db: Session) -> User:
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can manage logins.")
    return user


def _staff(db: Session, user_id: int) -> User | None:
    target = db.get(User, user_id)
    return None if target is None or target.role == "owner" else target


@router.post("/settings/users/create")
def create(request: Request, full_name: str = Form(default=""), username: str = Form(...), password: str = Form(...),
           role: str = Form(...), db: Session = Depends(get_db)):
    owner = _owner(request, db)
    name = username.strip().lower()
    if not USERNAME.fullmatch(name):
        return _back("username", True)
    if role not in ROLES:
        return _back("role", True)
    if not password_ok(password):
        return _back("password", True)
    if db.query(User).filter(User.username == name).first():
        return _back("taken", True)
    db.add(User(username=name, full_name=full_name.strip()[:128] or None, password_hash=hash_password(password),
                role=role, is_active=True, must_change_password=True))
    db.commit()
    log.info("staff_created by=%s role=%s", owner.id, role)
    return _back("created")


@router.post("/settings/users/{user_id}/reset-password")
def reset_password(user_id: int, request: Request, new_password: str = Form(...), db: Session = Depends(get_db)):
    owner = _owner(request, db)
    target = _staff(db, user_id)
    if target is None:
        return _back("missing", True)
    if not password_ok(new_password):
        return _back("password", True)
    target.password_hash, target.must_change_password = hash_password(new_password), True
    mfa = db.get(UserMfa, target.id)
    if mfa is not None:  # the usual reason for a reset is a lost phone
        db.delete(mfa)
    db.commit()
    log.info("staff_reset by=%s user=%s", owner.id, target.id)
    return _back("reset")


def _set_active(request: Request, db: Session, user_id: int, active: bool):
    owner = _owner(request, db)
    target = _staff(db, user_id)
    if target is None:
        return _back("missing", True)
    target.is_active = active
    db.commit()
    log.info("staff_active by=%s user=%s active=%s", owner.id, target.id, active)
    return _back("on" if active else "off")


@router.post("/settings/users/{user_id}/delete")
def turn_off(user_id: int, request: Request, db: Session = Depends(get_db)):
    return _set_active(request, db, user_id, False)


@router.post("/settings/users/{user_id}/activate")
def turn_on(user_id: int, request: Request, db: Session = Depends(get_db)):
    return _set_active(request, db, user_id, True)
