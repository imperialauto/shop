from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from app.config import SECRET_KEY
from app.database import get_db, init_db
from app.models import User
from app.auth import hash_password, verify_password, login_user, logout_user, get_current_user, after_login
from app.routes import customers, vehicles, repair_orders, invoices, ai_assist, webhooks, estimate_gen, sign, internal_messaging, alert_center, appointments, my_day, business_settings_page, dispatch, live, finance, owner_setup, public_site, staff
from app.routes import account_security
from app.scheduler import start_scheduler

import os
import time

from app.security import limiter
from app.permissions import ROLES, role_gate
from app import home_summary, preview_check, preview_seed
from app.public_site import preview_code
import hashlib
import hmac

app = FastAPI(title="Imperial Auto Care", dependencies=[Depends(role_gate)])

SESSION_DAYS = 7
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * SESSION_DAYS, same_site="lax",
                   https_only=bool(os.getenv("RAILWAY_ENVIRONMENT")))

# Static files
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# Include routers
app.include_router(customers.router, prefix="/customers", tags=["customers"])
app.include_router(appointments.router, prefix="/appointments", tags=["appointments"])
app.include_router(dispatch.router, prefix="/dispatch", tags=["dispatch"])
app.include_router(dispatch.internal_router, prefix="/internal/dispatch", tags=["internal"])
app.include_router(live.router, prefix="/live", tags=["live"])
app.include_router(vehicles.router, prefix="/vehicles", tags=["vehicles"])
app.include_router(repair_orders.router, prefix="/ro", tags=["repair_orders"])
app.include_router(invoices.router, prefix="/invoices", tags=["invoices"])
app.include_router(finance.router, prefix="/finance", tags=["finance"])
app.include_router(owner_setup.router, tags=["owner_setup"])
app.include_router(account_security.router, tags=["security"])
app.include_router(staff.router, tags=["staff"])
app.include_router(business_settings_page.router, tags=["settings"])
app.include_router(alert_center.router, tags=["alerts"])
app.include_router(my_day.router, tags=["my_day"])
app.include_router(public_site.router, tags=["public_site"])
app.include_router(ai_assist.router, prefix="/ai", tags=["ai"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(estimate_gen.router, prefix="/estimates", tags=["estimates"])
app.include_router(sign.router, prefix="/sign", tags=["sign"])
# Internal service-to-service API for the standalone messaging module (see
# app/routes/internal_messaging.py) — gated by INTERNAL_API_KEY, not a user
# session, since the caller is another Railway service, not a browser.
app.include_router(internal_messaging.router, prefix="/internal", tags=["internal"])


# Recovery startup is intentionally read-only: no schema mutation, default
# account creation, migrations, or customer follow-up scheduler.
from app.recovery import database_audit, read_only_enabled
from app.routes.internal_messaging import internal_key_valid
from fastapi.responses import JSONResponse
import logging
import json

app.state.read_only = read_only_enabled()

READ_ONLY_INTERNAL_WRITES = (
    "/internal/messaging/communications",
    "/internal/dispatch/intake",
    "/internal/dispatch/sync",
    "/internal/dispatch/tick",
    "/internal/dispatch/outbound",
    "/internal/dispatch/draft",
    "/internal/dispatch/handled",
    "/internal/dispatch/owner-action",
)


# Signing in and protecting your own login never touch shop records.
ACCOUNT_WRITES = ("/login", "/login/verify", "/settings/change-password", "/settings/security/start", "/settings/security/confirm", "/settings/security/disable")
# Service-to-service callers and public signature links do not come from a staff page.
CROSS_SITE_ALLOWED = ("/internal/", "/webhooks/")


def cross_site_post(request: Request) -> bool:
    """Browsers label where a form came from. Refuse staff writes started on another site."""
    if request.method in ("GET", "HEAD", "OPTIONS") or request.url.path.startswith(CROSS_SITE_ALLOWED):
        return False
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return origin == "null"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    return origin.split("://", 1)[-1] != host


def read_only_exempt(request: Request) -> bool:
    """Writes that stay open during recovery. Keep this list as short as possible.

    The Quo message mirror is exempt only for an authenticated internal caller:
    it appends to the communications log and has no outbound side effects, and
    blocking it silently drops customer conversations from their records.
    Dispatch routes touch only the queue tables, never jobs, invoices or payments.
    """
    if request.url.path in ACCOUNT_WRITES or request.url.path == "/dispatch/sending":
        return True  # the stop switch must work even while read-only
    if request.url.path == "/site/status":
        return True  # looks up a status link and writes nothing
    if request.url.path in ("/settings/business", "/alerts"):
        return True  # owner configuration, audited, checks for the owner itself
    if request.url.path.startswith("/settings/users/"):
        return True  # logins only; these routes check for the owner themselves
    # The one-time owner claim has its own strong Railway secret + CSRF
    # checks and only updates the sole user credential, not customer data.
    if (request.url.path == "/owner-setup"
            and request.method == "POST"
            and len(os.getenv("IMPERIAL_OWNER_SETUP_TOKEN", "")) >= 32):
        return True
    return (
        request.method == "POST"
        and request.url.path in READ_ONLY_INTERNAL_WRITES
        and internal_key_valid(request.headers.get("x-internal-key"))
    )


PREVIEW_COOKIE = "imperial_preview"
PREVIEW_FORM = ('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Private preview</title>'
                '<body style="background:#2D0245;color:#fff;font-family:-apple-system,sans-serif;text-align:center;padding:64px 20px"><h1 style="font-size:22px">Private preview</h1>{error}'
                '<form method="post" action="/preview-access"><input name="code" type="password" aria-label="Access code" autocomplete="off" style="font-size:16px;padding:12px;width:260px;max-width:100%">'
                '<br><button type="submit" style="margin-top:14px;min-height:48px;padding:0 28px;font-size:16px;font-weight:700;background:#EAD272;border:0;border-radius:10px">Continue</button></form>')


async def preview_gate(request: Request):
    """Only a disposable preview has a code. Every other deployment skips this."""
    code = preview_code()
    if not code or request.url.path in ("/healthz", "/readyz"):
        return None
    expected = hashlib.sha256(code.encode()).hexdigest()
    if hmac.compare_digest(request.cookies.get(PREVIEW_COOKIE, ""), expected):
        return None
    if request.url.path != "/preview-access":
        return RedirectResponse("/preview-access", status_code=303)
    if request.method == "POST":
        entered = str((await request.form()).get("code", ""))
        if hmac.compare_digest(entered.encode(), code.encode()):
            response = RedirectResponse("/site/", status_code=303)
            response.set_cookie(PREVIEW_COOKIE, expected, max_age=7 * 86400, httponly=True, secure=True, samesite="lax")
            return response
        return HTMLResponse(PREVIEW_FORM.format(error="<p>That code did not match.</p>"), status_code=403, headers={"X-Robots-Tag": "noindex, nofollow"})
    return HTMLResponse(PREVIEW_FORM.format(error=""), headers={"X-Robots-Tag": "noindex, nofollow"})


@app.middleware("http")
async def recovery_mode(request: Request, call_next):
    gated = await preview_gate(request)
    if gated is not None:
        return gated
    if cross_site_post(request):
        return JSONResponse({"error": "This request came from another site."}, status_code=403)
    if app.state.read_only and request.method not in ("GET", "HEAD", "OPTIONS") and not read_only_exempt(request):
        if "text/html" in request.headers.get("accept", ""):
            return _message_page("Saving is paused", "The system is in read-only mode, so nothing was changed.", 503)
        return JSONResponse({"error": "Recovery audit mode: production changes and external actions are paused.", "read_only": True}, status_code=503)
    response = await call_next(request)
    if preview_code():
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/live/overlay"):
        response.headers.setdefault("X-Frame-Options", "DENY")
    return response

@app.on_event("startup")
def startup():
    preview_seed.seed()
    preview_check.start()
    audit = database_audit()
    app.state.database_audit = audit
    logging.getLogger("uvicorn.error").info("imperial_database_audit %s", json.dumps(audit, sort_keys=True))

def _message_page(title: str, detail: str, status: int) -> HTMLResponse:
    return HTMLResponse('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">'
                        f'<title>{title}</title><body style="background:#09090b;color:#e4e4e7;font-family:-apple-system,sans-serif;text-align:center;padding:64px 20px">'
                        f'<h1 style="font-size:20px">{title}</h1><p style="color:#a1a1aa">{detail}</p>'
                        '<a href="javascript:history.back()" style="color:#d4a853">Go back</a></body>', status_code=status)


@app.exception_handler(401)
def not_signed_in(request: Request, exc):
    """A person in a browser goes to sign in, then back to the page they wanted."""
    if request.method != "GET" or "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=401)
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    request.session["next"] = path[:300]
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(403)
def forbidden(request: Request, exc):
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=403)
    return _message_page("Not available on your login", "Ask the owner if you need access to this.", 403)


@app.exception_handler(404)
def not_found(request: Request, exc):
    if "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return HTMLResponse('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Not found</title>'
                        '<body style="background:#09090b;color:#e4e4e7;font-family:-apple-system,sans-serif;text-align:center;padding:64px 20px">'
                        '<h1 style="font-size:20px">That page could not be found</h1><p style="color:#a1a1aa">It may have moved or been removed.</p>'
                        '<a href="/" style="color:#d4a853">Back to Imperial Operations</a></body>', status_code=404)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "mode": "read-only" if app.state.read_only else "active"}

@app.get("/readyz")
def readyz():
    audit = database_audit(include_counts=False)
    ready = audit["connected"] and not audit.get("error_type") and not audit.get("missing_tables") and not audit.get("missing_columns")
    return JSONResponse({"status": "ready" if ready else "not_ready", "database_connected": audit["connected"], "mode": "read-only" if app.state.read_only else "active"}, status_code=200 if ready else 503)


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    keys = (f"user:{username.strip().lower()}", account_security.client_key(request))
    if limiter.locked(*keys):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many tries. Wait 15 minutes, then try again."}, status_code=429)
    user = db.query(User).filter(User.username == username).first()
    if password == "imperial2024" or not user or user.is_active is False or not verify_password(password, user.password_hash):
        limiter.record(*keys)
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})
    limiter.clear(*keys)
    if account_security.confirmed_mfa(db, user.id):
        wanted = request.session.get("next")
        request.session.clear()
        request.session["pending_mfa"] = {"user": user.id, "at": time.time()}
        if wanted:
            request.session["next"] = wanted  # still go there after the code
        return RedirectResponse("/login/verify", status_code=303)
    login_user(request, user)
    return RedirectResponse(after_login(request, "/my-day" if user.role == "tech" else "/dashboard"), status_code=302)


@app.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, view: str = "open", db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    from datetime import datetime
    from app.appointments import PHOENIX
    from app.models import Customer, Invoice, RepairOrder
    open_ros = db.query(RepairOrder).filter(RepairOrder.status != "delivered").order_by(RepairOrder.created_at.desc()).limit(10).all()
    total_customers = db.query(Customer).count()
    total_ros = db.query(RepairOrder).count()
    open_count = db.query(RepairOrder).filter(RepairOrder.status != "delivered").count()

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "user": user,
        "open_ros": open_ros,
        "total_customers": total_customers,
        "total_ros": total_ros,
        "open_count": open_count,
        "total_invoices": db.query(Invoice).filter(Invoice.status == "sent").count(),
        "now": datetime.now(PHOENIX),
        **dispatch.dashboard_context(db, view),
        "home": home_summary.summary(db, user),
        "read_only": app.state.read_only,
    })


def _settings(request: Request, db: Session, user: User, **extra):
    query = request.query_params
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "roles": ROLES,
        "users": db.query(User).order_by(User.id).all() if user.role == "owner" else None,
        "user_success": staff.MESSAGES.get(query.get("user_saved")), "user_error": staff.ERRORS.get(query.get("user_error")),
        "must_change": bool(user.must_change_password), **extra})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
    return _settings(request, db, user)


@app.post("/settings/change-password")
def change_password(
    request: Request,
    db: Session = Depends(get_db),
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
    if not verify_password(current_password, user.password_hash):
        return _settings(request, db, user, error="Current password is incorrect")
    if not staff.password_ok(new_password):
        return _settings(request, db, user, error=staff.ERRORS["password"])
    user.password_hash, user.must_change_password = hash_password(new_password), False
    db.commit()
    login_user(request, user)  # owner sessions are tied to the password
    return _settings(request, db, user, success="Password updated!")


@app.get("/status/{token}", response_class=HTMLResponse)
def public_status(request: Request, token: str, db: Session = Depends(get_db)):
    from app.models import RepairOrder
    ro = db.query(RepairOrder).filter(RepairOrder.public_token == token).first()
    if not ro:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse("public_status.html", {"request": request, "ro": ro})

