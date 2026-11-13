import bcrypt
import hashlib
import hmac
from app.config import SECRET_KEY
from fastapi import Request, HTTPException
from sqlalchemy.orm import Session
from app.models import User


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _owner_session_tag(user: User) -> str:
    """Bind an owner session to the current password hash without disclosing it."""
    material = f"{user.id}:{user.password_hash}".encode("utf-8")
    return hmac.new(SECRET_KEY.encode("utf-8"), material, hashlib.sha256).hexdigest()


def after_login(request: Request, default: str) -> str:
    """The page someone was sent away from, if it is a local page, else the default."""
    target = request.session.pop("next", "") or ""
    return target if target.startswith("/") and not target.startswith("//") and "\\" not in target else default


def get_current_user(request: Request, db: Session) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if session_too_old(request):
        request.session.clear()
        raise HTTPException(status_code=401, detail="Signed out after the time set in Security & Privacy.")
    if user.role == "owner":
        provided = request.session.get("owner_session_tag", "")
        if not provided or not hmac.compare_digest(provided, _owner_session_tag(user)):
            raise HTTPException(status_code=401, detail="Owner session expired. Sign in again.")
    return user


def require_admin(user: User):
    """Raise 403 unless the given user has the admin role. Use inside routes
    that manage other user accounts (creating/removing logins, resetting
    passwords for someone else)."""
    if user.role not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin access required")


def session_too_old(request: Request, now: float | None = None) -> bool:
    """Older sessions without a sign-in time start counting from now."""
    import time
    from app import security_controls
    moment = time.time() if now is None else now
    started = request.session.get("login_at")
    if not isinstance(started, (int, float)):
        request.session["login_at"] = moment
        return False
    try:
        hours = security_controls.value("session_hours")
    except Exception:
        hours = 168
    return moment - started > hours * 3600


def login_user(request: Request, user: User):
    import time
    request.session["login_at"] = time.time()
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    if user.role == "owner":
        request.session["owner_session_tag"] = _owner_session_tag(user)
    else:
        request.session.pop("owner_session_tag", None)


def logout_user(request: Request):
    request.session.clear()
