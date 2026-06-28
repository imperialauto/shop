from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import RepairOrder, Customer, Vehicle, User, LineItem
from app.auth import get_current_user
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


def require_user(request, db):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


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
    techs = db.query(User).all()
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
    techs = db.query(User).all()
    subtotal = sum(li.total for li in ro.line_items)
    return templates.TemplateResponse("repair_orders/detail.html", {
        "request": request, "user": user, "ro": ro, "techs": techs,
        "statuses": RO_STATUSES, "subtotal": subtotal,
    })


@router.post("/{ro_id}/update-status")
def update_status(request: Request, ro_id: int, db: Session = Depends(get_db), status: str = Form(...)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)
    ro.status = status
    db.commit()
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
    opts = "".join(f'<option value="{v.id}">{v.year} {v.make} {v.model}</option>' for v in vehicles)
    return HTMLResponse(f'<select name="vehicle_id" class="form-select">{opts}</select>')
