from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from app.config import SECRET_KEY
from app.database import get_db, init_db
from app.models import User
from app.auth import hash_password, verify_password, login_user, logout_user, get_current_user, require_admin
from app.routes import customers, vehicles, repair_orders, invoices, ai_assist, webhooks, estimate_gen, sign
from app.scheduler import start_scheduler

import os

app = FastAPI(title="Imperial Auto Care")

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=86400 * 30)

# Static files
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# Include routers
app.include_router(customers.router, prefix="/customers", tags=["customers"])
app.include_router(vehicles.router, prefix="/vehicles", tags=["vehicles"])
app.include_router(repair_orders.router, prefix="/ro", tags=["repair_orders"])
app.include_router(invoices.router, prefix="/invoices", tags=["invoices"])
app.include_router(ai_assist.router, prefix="/ai", tags=["ai"])
app.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
app.include_router(estimate_gen.router, prefix="/estimates", tags=["estimates"])
app.include_router(sign.router, prefix="/sign", tags=["sign"])


@app.on_event("startup")
def startup():
    init_db()
    # Safe column migrations — add new columns without a full migration framework
    from app.database import SessionLocal, engine
    import sqlalchemy as sa
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE repair_orders ADD COLUMN IF NOT EXISTS signature_data TEXT",
            "ALTER TABLE repair_orders ADD COLUMN IF NOT EXISTS approved_by VARCHAR(128)",
            "ALTER TABLE repair_orders ADD COLUMN IF NOT EXISTS signed_at TIMESTAMP",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_signature TEXT",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_approved_by VARCHAR(128)",
            "ALTER TABLE invoices ADD COLUMN IF NOT EXISTS customer_signed_at TIMESTAMP",
            "ALTER TABLE estimate_sessions ADD COLUMN IF NOT EXISTS follow_up_sent BOOLEAN DEFAULT FALSE",
        ]:
            try:
                conn.execute(sa.text(stmt))
                conn.commit()
            except Exception:
                pass  # Column already exists

    # Start background scheduler (follow-up jobs, etc.)
    start_scheduler()

    # Create default admin if no users exist
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                password_hash=hash_password("imperial2024"),
                full_name="Admin",
                role="admin"
            )
            db.add(admin)
            db.commit()
    finally:
        db.close()


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
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid username or password"})
    login_user(request, user)
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    from app.models import RepairOrder, Customer
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
    })


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
    users = None
    if user.role == "admin":
        users = db.query(User).order_by(User.created_at.asc()).all()
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "users": users})


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
        return templates.TemplateResponse("settings.html", {"request": request, "user": user, "error": "Current password is incorrect"})
    user.password_hash = hash_password(new_password)
    db.commit()
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "success": "Password updated!"})


@app.post("/settings/users/create")
def create_user(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(...),
    full_name: str = Form(""),
    password: str = Form(...),
    role: str = Form("office"),
):
    try:
        user = get_current_user(request, db)
        require_admin(user)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    users = db.query(User).order_by(User.created_at.asc()).all()
    username_clean = username.strip().lower()

    if not username_clean or not password:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "Username and password are required.",
        })

    if len(password) < 6:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "Password must be at least 6 characters.",
        })

    if db.query(User).filter(User.username == username_clean).first():
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": f"Username '{username_clean}' is already taken.",
        })

    if role not in ("admin", "office", "tech"):
        role = "office"

    new_user = User(
        username=username_clean,
        password_hash=hash_password(password),
        full_name=full_name.strip() or username_clean,
        role=role,
    )
    db.add(new_user)
    db.commit()

    users = db.query(User).order_by(User.created_at.asc()).all()
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "users": users,
        "user_success": f"Created login for {new_user.full_name} (username: {new_user.username}).",
    })


@app.post("/settings/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    new_password: str = Form(...),
):
    try:
        user = get_current_user(request, db)
        require_admin(user)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    target = db.query(User).filter(User.id == user_id).first()
    users = db.query(User).order_by(User.created_at.asc()).all()

    if not target:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "User not found.",
        })

    if len(new_password) < 6:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "Password must be at least 6 characters.",
        })

    target.password_hash = hash_password(new_password)
    db.commit()
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "users": users,
        "user_success": f"Password reset for {target.full_name or target.username}.",
    })


@app.post("/settings/users/{user_id}/delete")
def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        user = get_current_user(request, db)
        require_admin(user)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    users = db.query(User).order_by(User.created_at.asc()).all()
    target = db.query(User).filter(User.id == user_id).first()

    if not target:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "User not found.",
        })

    if target.id == user.id:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "You can't remove the account you're currently logged in as.",
        })

    admin_count = db.query(User).filter(User.role == "admin").count()
    if target.role == "admin" and admin_count <= 1:
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "users": users,
            "user_error": "Can't remove the last admin account.",
        })

    db.delete(target)
    db.commit()
    users = db.query(User).order_by(User.created_at.asc()).all()
    return templates.TemplateResponse("settings.html", {
        "request": request, "user": user, "users": users,
        "user_success": "User removed.",
    })


@app.get("/status/{token}", response_class=HTMLResponse)
def public_status(request: Request, token: str, db: Session = Depends(get_db)):
    from app.models import RepairOrder
    ro = db.query(RepairOrder).filter(RepairOrder.public_token == token).first()
    if not ro:
        raise HTTPException(status_code=404, detail="Not found")
    return templates.TemplateResponse("public_status.html", {"request": request, "ro": ro})
