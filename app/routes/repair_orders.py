from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import RepairOrder, Customer, Vehicle, User, LineItem, Communication, PartsOrder
from app.auth import get_current_user
from app.config import GOOGLE_REVIEW_LINK
from app import approval_links, customer_file, cycle_time, next_action, website_alerts
import os, secrets

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

COMMON_CONCERNS = [
    "Check engine light on",
    "No start / hard start",
    "Oil leak",
    "Transmission slipping",
    "Overheating",
    "Brake inspection",
    "Suspension noise",
    "AC not cooling",
    "Exhaust smoke",
    "DPF / DEF issue",
    "Turbo noise / no boost",
    "Glow plug issue",
    "Fuel system concern",
    "State inspection",
    "Oil change / PM service",
]

RO_STATUSES = [
    ("intake", "Intake"),
    ("diagnosing", "Diagnosing"),
    ("waiting_parts", "Waiting Parts"),
    ("in_progress", "In Progress"),
    ("waiting_approval", "Waiting Approval"),
    ("complete", "Complete"),
    ("delivered", "Delivered"),
]


def workers(db):
    """Active logins that can be assigned a job. Office logins don't work on vehicles."""
    return [u for u in db.query(User).order_by(User.full_name, User.username).all()
            if u.role in ("tech", "owner", "admin") and u.is_active is not False]


def require_user(request, db):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def get_next_action(ro, open_parts_count=0, has_unpaid_invoice=False):
    if ro.status == "intake":
        return "Needs diagnosis assigned"
    if ro.status == "diagnosing":
        return "Diagnosis in progress"
    if ro.status == "waiting_parts":
        return f"Waiting on {open_parts_count} part(s)" if open_parts_count else "Review parts readiness"
    if ro.status == "in_progress":
        return "Repair in progress"
    if ro.status == "waiting_approval":
        return "Review signed scope before work" if ro.signed_at else "Waiting on customer signature"
    if ro.status == "complete":
        return "Review payment balance" if has_unpaid_invoice else "Review pickup"
    return "Delivered" if ro.status == "delivered" else "Review job"


@router.get("/board", response_class=HTMLResponse)
def board_view(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ros = db.query(RepairOrder).filter(RepairOrder.status != "delivered").order_by(RepairOrder.created_at.asc()).all()
    ids = [ro.id for ro in ros]
    counts = dict(db.query(PartsOrder.repair_order_id, func.count(PartsOrder.id)).filter(PartsOrder.repair_order_id.in_(ids), PartsOrder.status == "ordered").group_by(PartsOrder.repair_order_id).all()) if ids else {}
    columns = {key: [] for key, _ in RO_STATUSES if key != "delivered"}
    for ro in ros:
        step = next_action.for_job(db, ro, user.role)  # same answer as the job page, one source
        ro.next_action = f"{step['who']}: {step['title']}" if step else get_next_action(ro, counts.get(ro.id, 0), any(i.status != "paid" for i in ro.invoices))
        columns.setdefault(ro.status, []).append(ro)
    return templates.TemplateResponse("repair_orders/board.html", {"request": request, "user": user, "columns": columns, "statuses": RO_STATUSES})


@router.get("/", response_class=HTMLResponse)
def list_ros(request: Request, db: Session = Depends(get_db), status: str = "open", search: str = ""):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    query = db.query(RepairOrder)
    if status == "open":
        query = query.filter(RepairOrder.status != "delivered")
    elif status == "delivered":
        query = query.filter(RepairOrder.status == "delivered")
    if search:
        query = query.join(Customer).filter(
            (Customer.first_name.ilike(f"%{search}%")) |
            (Customer.last_name.ilike(f"%{search}%")) |
            (Customer.fleet_name.ilike(f"%{search}%"))
        )

    ros = query.order_by(RepairOrder.created_at.desc()).all()
    return templates.TemplateResponse("repair_orders/list.html", {
        "request": request, "user": user, "ros": ros,
        "status_filter": status, "search": search, "statuses": RO_STATUSES,
    })


@router.get("/new", response_class=HTMLResponse)
def new_ro_form(request: Request, customer_id: int = None, vehicle_id: int = None, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customers = db.query(Customer).order_by(Customer.last_name).all()
    vehicles = []
    if customer_id:
        vehicles = db.query(Vehicle).filter(Vehicle.customer_id == customer_id).all()
    techs = workers(db)
    return templates.TemplateResponse("repair_orders/form.html", {
        "request": request, "user": user, "ro": None,
        "customers": customers, "vehicles": vehicles, "techs": techs,
        "selected_customer_id": customer_id, "selected_vehicle_id": vehicle_id,
        "common_concerns": COMMON_CONCERNS,
    })


@router.post("/new")
def create_ro(
    request: Request,
    db: Session = Depends(get_db),
    customer_id: int = Form(...),
    vehicle_id: int = Form(...),
    assigned_tech_id: int = Form(None),
    concern: str = Form(""),
    mileage_in: int = Form(None),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    ro = RepairOrder(
        customer_id=customer_id,
        vehicle_id=vehicle_id,
        assigned_tech_id=assigned_tech_id or None,
        concern=concern or None,
        mileage_in=mileage_in,
        public_token=secrets.token_urlsafe(32),
    )
    db.add(ro)
    db.commit()
    db.refresh(ro)
    return RedirectResponse(f"/ro/{ro.id}", status_code=302)


@router.get("/{ro_id}", response_class=HTMLResponse)
def view_ro(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)
    techs = workers(db)
    subtotal = sum(li.total for li in ro.line_items if li.billable)
    # Pull all communications for this customer (across all ROs), sorted oldest→newest
    comms = (
        db.query(Communication)
        .filter(Communication.customer_id == ro.customer_id)
        .order_by(Communication.created_at.asc())
        .all()
    )
    return templates.TemplateResponse("repair_orders/detail.html", {
        "request": request, "user": user, "ro": ro, "techs": techs,
        "statuses": RO_STATUSES, "subtotal": subtotal, "comms": comms,
        "next_step": next_action.for_job(db, ro, user.role),
        "timeline": cycle_time.for_job(db, ro) if user.role in ("owner", "admin") else None,
        "open_invoice": next((i for i in ro.invoices if i.status != "void"), None),
    })


async def _send_review_request(phone: str, first_name: str):
    """Background task: send Google review request after delivery."""
    from app.routes.webhooks import send_sms
    msg = (
        f"Hi {first_name}! Thanks for choosing Imperial Auto Care — we really appreciate your business. "
        f"If you have 30 seconds, a Google review helps us out more than you know: {GOOGLE_REVIEW_LINK} 🙏"
    )
    await send_sms(phone, msg)


@router.post("/{ro_id}/update-status")
def update_status(
    request: Request,
    ro_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    status: str = Form(...),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)

    if status not in dict(RO_STATUSES):
        raise HTTPException(status_code=422, detail="Unknown status.")
    if user.role == "tech" and status == "delivered":
        raise HTTPException(status_code=403, detail="The front desk marks a job delivered.")
    prev_status = ro.status
    ro.status = status
    if status != "intake":
        website_alerts.close_for_job(db, ro, user.username, f"job moved to {status}")
    db.commit()

    # Auto-send review request when marked delivered (only once)
    if status == "delivered" and prev_status != "delivered" and os.getenv("REVIEW_REQUESTS_ENABLED") == "true":
        if ro.customer and ro.customer.phone and GOOGLE_REVIEW_LINK:
            background_tasks.add_task(
                _send_review_request,
                ro.customer.phone,
                ro.customer.first_name or "there",
            )
            print(f"[Review] Queued review request to {ro.customer.phone} for RO #{ro_id}")

    back = request.query_params.get("back")
    return RedirectResponse("/my-day" if back == "my-day" else f"/ro/{ro_id}", status_code=302)


@router.post("/{ro_id}/request-approval")
def request_approval(request: Request, ro_id: int, db: Session = Depends(get_db)):
    """Marks undecided lines as waiting on the customer and queues the approval
    text for the owner's YES. Nothing is sent from here."""
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.get(RepairOrder, ro_id)
    if not ro:
        raise HTTPException(status_code=404)
    fresh = [li for li in ro.line_items if li.approval is None]
    for li in fresh:
        li.approval = "pending"
    if fresh:
        ro.status = "waiting_approval"
        website_alerts.close_for_job(db, ro, user.username, "approval requested")
    db.commit()
    link = "none"
    phone = approval_links.mobile(ro.customer.phone)
    if phone is None:
        link = "no_phone"
    elif any(li.approval == "pending" for li in ro.line_items):
        thread, problem = customer_file._bridge("/conversation", phone[-10:])
        if problem:
            link = "unavailable"
        else:
            messages = thread.get("messages") or []
            newest = messages[-1].get("id") if messages else "none"
            item = approval_links.queue(db, ro, newest or "none", user.username)
            db.commit()
            link = f"queued{item.id}"
    return RedirectResponse(f"/ro/{ro_id}?asked={len(fresh)}&link={link}", status_code=302)


@router.post("/{ro_id}/assign")
def assign(request: Request, ro_id: int, db: Session = Depends(get_db), assigned_tech_id: str = Form("")):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.get(RepairOrder, ro_id)
    if not ro:
        raise HTTPException(status_code=404)
    if assigned_tech_id:
        worker = db.get(User, int(assigned_tech_id)) if assigned_tech_id.isdigit() else None
        if worker is None or worker not in workers(db):
            raise HTTPException(status_code=422, detail="Choose someone who works on vehicles.")
        ro.assigned_tech_id = worker.id
    else:
        ro.assigned_tech_id = None
    db.commit()
    if request.query_params.get("back") == "my-day":
        return RedirectResponse("/my-day", status_code=302)
    return RedirectResponse(f"/ro/{ro_id}", status_code=302)


@router.post("/{ro_id}/update-notes")
def update_notes(request: Request, ro_id: int, db: Session = Depends(get_db), tech_notes: str = Form("")):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)
    ro.tech_notes = tech_notes
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}", status_code=302)


@router.post("/{ro_id}/add-line-item")
def add_line_item(
    request: Request,
    ro_id: int,
    db: Session = Depends(get_db),
    description: str = Form(...),
    item_type: str = Form("labor"),
    quantity: float = Form(1.0),
    unit_price: float = Form(0.0),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    li = LineItem(
        repair_order_id=ro_id,
        description=description,
        item_type=item_type,
        quantity=quantity,
        unit_price=unit_price,
    )
    db.add(li)
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}", status_code=302)


@router.post("/{ro_id}/delete-line-item/{li_id}")
def delete_line_item(request: Request, ro_id: int, li_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    li = db.query(LineItem).filter(LineItem.id == li_id, LineItem.repair_order_id == ro_id).first()
    if li:
        db.delete(li)
        db.commit()
    return RedirectResponse(f"/ro/{ro_id}", status_code=302)


@router.get("/{ro_id}/vehicles", response_class=HTMLResponse)
def get_vehicles_for_customer(request: Request, ro_id: int, customer_id: int, db: Session = Depends(get_db)):
    """HTMX endpoint — returns vehicle options for a selected customer"""
    vehicles = db.query(Vehicle).filter(Vehicle.customer_id == customer_id).all()

    add_link = (
        f'<a href="/vehicles/new?customer_id={customer_id}&next=job" '
        f'class="btn-primary text-xs" style="white-space:nowrap; padding:6px 10px;">+ Add Vehicle</a>'
    )

    if not vehicles:
        return HTMLResponse(
            f'<div style="display:flex;gap:8px;align-items:center;">'
            f'<select name="vehicle_id" class="form-select" required>'
            f'<option value="">No vehicles on file — add one first</option>'
            f'</select>'
            f'{add_link}'
            f'</div>'
        )

    opts = '<option value="">Select vehicle…</option>'
    for v in vehicles:
        label = f"{v.year} {v.make} {v.model}"
        if v.engine:
            label += f" — {v.engine}"
        if v.license_plate:
            label += f" · {v.license_plate}"
        opts += f'<option value="{v.id}">{label}</option>'

    add_btn = (
        f'<a href="/vehicles/new?customer_id={customer_id}&next=job" '
        f'class="btn-secondary text-xs" style="white-space:nowrap; padding:6px 10px;">+ Add</a>'
    )
    return HTMLResponse(
        f'<div style="display:flex;gap:8px;align-items:center;">'
        f'<select name="vehicle_id" class="form-select" required style="flex:1;">{opts}</select>'
        f'{add_btn}'
        f'</div>'
    )

