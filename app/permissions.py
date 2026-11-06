"""What each staff role can reach. Anything not listed is refused."""
import re

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

ROLES = {"admin": "Admin", "office": "Office Coordinator", "tech": "Technician"}
PUBLIC = ("/login", "/logout", "/status", "/sign", "/webhooks", "/internal", "/healthz", "/readyz", "/live/overlay", "/owner-setup")
OWNER_ONLY = ("/finance", "/settings/users")
ADMIN_ONLY = ("/live", "/dispatch/sending", "/dispatch/policies")
OFFICE = ("/dashboard", "/customers", "/vehicles", "/ro", "/appointments", "/invoices", "/estimates", "/dispatch", "/ai", "/settings")
TECH_READ = re.compile(r"^/(ro(/board|/|/\d+(/vehicles)?)?|vehicles/\d+|appointments/?|appointments/calendar|settings(/security)?)$")
TECH_WRITE = re.compile(r"^/(ro/\d+/(update-status|update-notes|add-line-item)|ai/[a-z-]+/\d+|settings/change-password|settings/security/(start|confirm|disable))$")
PASSWORD_CHANGE_PATHS = ("/settings", "/settings/change-password", "/logout")


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def allowed(role: str | None, method: str, path: str) -> bool:
    if path == "/" or _under(path, PUBLIC) or role == "owner":
        return True
    if _under(path, OWNER_ONLY):
        return False
    if role == "admin":
        return True
    if _under(path, ADMIN_ONLY):
        return False
    if role == "office":
        return _under(path, OFFICE)
    return bool((TECH_READ if method in ("GET", "HEAD") else TECH_WRITE).match(path))


def _redirect(location: str) -> HTTPException:
    return HTTPException(status_code=303, headers={"Location": location})


def role_gate(request: Request, db: Session = Depends(get_db)):
    """Runs before every page. Each route still checks that someone is signed in."""
    user_id = request.session.get("user_id")
    if not user_id:
        return
    user = db.get(User, user_id)
    if user is None:
        return
    path = request.url.path
    if user.is_active is False:
        request.session.clear()
        raise _redirect("/login")
    if user.must_change_password and not _under(path, PUBLIC) and path not in PASSWORD_CHANGE_PATHS:
        raise _redirect("/settings?change=1")
    if user.role == "tech" and path == "/dashboard":
        raise _redirect("/ro/")
    if not allowed(user.role, request.method, path):
        raise HTTPException(status_code=403, detail="Your login doesn't have access to this.")
