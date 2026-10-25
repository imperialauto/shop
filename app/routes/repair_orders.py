from fastapi import APIRouter, Request, Depends, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import RepairOrder, Customer, Vehicle, User, LineItem, Communication, PartsOrder
from app.auth import get_current_user
from app.config import GOOGLE_REVIEW_LINK
import os
import secrets
from html import escape

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

COMMON_CONCERNS = [
    "Check engine light on", "No start / hard start", "Oil leak",
    "Transmission slipping", "Overheating", "Brake inspection",
    "Suspension noise", "AC not cooling", "Exhaust smoke", "DPF / DEF issue",
    "Turbo noise / no boost", "Glow plug issue", "Fuel system concern",
    "State inspection", "Oil change / PM service",
]
RO_STATUSES = [
    ("intake", "Intake"), ("diagnosing", "Diagnosing"),
    ("waiting_parts", "Waiting Parts"), ("in_progress", "In Progress"),
    ("waiting_approval", "Waiting Approval"), ("complete", "Complete"),
    ("delivered", "Delivered"),
]
VALID_STATUSES = {key for key, _ in RO_STATUSES}

def require_user(request, db):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None

def get_next_action(ro, open_parts_count=0, has_unpaid_invoice=False):
    """Short, human-readable flag for what's blocking this RO."""
    if ro.status == "intake":
        return "Needs diagnosis assigned"
    if ro.status == "diagnosing":
        return "Diagnosis in progress"
    if ro.status == "waiting_parts":
        if open_parts_count:
            return f"Waiting on {open_parts_count} part{'s' if open_parts_count != 1 else ''}"
        return "Parts in — move to in progress"
    if ro.status == "in_progress":
        return "Repair in progress"
    if ro.status == "waiting_approval":
        return "Signed — advance to in progress" if ro.signed_at else "Waiting on customer signature"
    if ro.status == "complete":
        return "Collect payment" if has_unpaid_invoice else "Ready for pickup"
    if ro.status == "delivered":
        return "Delivered"
    return ""

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

@router.get("/board", response_class=HTMLResponse)
def board_view(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    ros = (db.query(RepairOrder)
           .filter(RepairOrder.status != "delivered")
           .order_by(RepairOrder.created_at.asc()).all())
    ro_ids = [ro.id for ro in ros]
    open_parts_counts = {}
    if ro_ids:
        rows = (db.query(PartsOrder.repair_order_id, func.count(PartsOrder.id))
                .filter(PartsOrder.repair_order_id.in_(ro_ids), PartsOrder.status == "ordered")
                .group_by(PartsOrder.repair_order_id).all())
        open_parts_counts = dict(rows)
    columns = {key: [] for key, _ in RO_STATUSES if key != "delivered"}
    for ro in ros:
        has_unpaid = any(i.status != "paid" for i in ro.invoices)
        open_parts = open_parts_counts.get(ro.id, 0)
        ro.next_action = get_next_action(ro, open_parts, has_unpaid)
        columns.setdefault(ro.status, []).append(ro)
    counts = {key: len(columns.get(key, [])) for key, _ in RO_STATUSES if key != "delivered"}
    return templates.TemplateResponse("repair_orders/board.html", {
        "request": request, "user": user,
        "columns": columns, "statuses": RO_STATUSES, "counts": counts,
    })

@router.get("/new", response_class=HTMLResponse)
def new_ro_form(request: Request, customer_id: int = None, vehicle_id: int = None, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customers = db.query(Customer).order_by(Customer.last_name).all()
    vehicles = db.query(Vehicle).filter(Vehicle.customer_id == customer_id).all() if customer_id else []
    techs = db.query(User).all()
    return templates.TemplateResponse("repair_orders/form.html", {
        "request": request, "user": user, "ro": None,
        "customers": customers, "vehicles": vehicles, "techs": techs,
        "selected_customer_id": customer_id, "selected_vehicle_id": vehicle_id,
        "common_concerns": COMMON_CONCERNS,
    })

@router.post("/new")
def create_ro(request: Request, db: Session = Depends(get_db),
              customer_id: int = Form(...), vehicle_id: int = Form(...),
              assigned_tech_id: int = Form(None), concern: str = Form(""),
              mileage_in: int = Form(None)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id, Vehicle.customer_id == customer_id).first()
    if not vehicle:
        raise HTTPException(status_code=400, detail="Vehicle does not belong to selected customer")
    ro = RepairOrder(customer_id=customer_id, vehicle_id=vehicle_id,
                     assigned_tech_id=assigned_tech_id or None,
                     concern=concern or None, mileage_in=mileage_in,
                     public_token=secrets.token_urlsafe(32))
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
    comms = (db.query(Communication).filter(Communication.customer_id == ro.customer_id)
             .order_by(Communication.created_at.asc()).all())
    return templates.TemplateResponse("repair_orders/detail.html", {
        "request": request, "user": user, "ro": ro, "techs": techs,
        "statuses": RO_STATUSES, "subtotal": subtotal, "comms": comms,
    })

async def _send_review_request(phone: str, first_name: str):
    from app.routes.webhooks import send_sms
    msg = (f"Hi {first_name}! Thanks for choosing Imperial Auto Care — we really appreciate your business. "
           f"If you have 30 seconds, a Google review helps us out more than you know: {GOOGLE_REVIEW_LINK}")
    await send_sms(phone, msg)

@router.post("/{ro_id}/update-status")
def update_status(request: Request, ro_id: int, background_tasks: BackgroundTasks,
                  db: Session = Depends(get_db), status: str = Form(...)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid repair order status")
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        raise HTTPException(status_code=404)
    prev_status = ro.status
    ro.status = status
    db.commit()
    if status == "delivered" and prev_status != "delivered":
        if ro.customer and ro.customer.phone and GOOGLE_REVIEW_LINK:
            background_tasks.add_task(_send_review_request, ro.customer.phone, ro.customer.first_name or "there")
    return RedirectResponse(f"/ro/{ro_id}", status_code=302)

@router.post("/{ro_id}/update-notes")
def update_notes(request: Request, ro_id: int, db: Session = Depends(get_db),
                 tech_notes: str = Form("")):
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
def add_line_item(request: Request, ro_id: int, db: Session = Depends(get_db),
                  description: str = Form(...), item_type: str = Form("labor"),
                  quantity: float = Form(1.0), unit_price: float = Form(0.0)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if not db.query(RepairOrder.id).filter(RepairOrder.id == ro_id).first():
        raise HTTPException(status_code=404)
    if quantity <= 0 or unit_price < 0:
        raise HTTPException(status_code=400, detail="Invalid line item amount")
    li = LineItem(repair_order_id=ro_id, description=description,
                  item_type=item_type, quantity=quantity, unit_price=unit_price)
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
def get_vehicles_for_customer(request: Request, ro_id: int, customer_id: int,
                              db: Session = Depends(get_db)):
    if not require_user(request, db):
        return RedirectResponse("/login", status_code=302)
    vehicles = db.query(Vehicle).filter(Vehicle.customer_id == customer_id).all()
    add_link = (f'<a href="/vehicles/new?customer_id={customer_id}" target="_blank" '
                f'class="btn-primary text-xs" style="white-space:nowrap; padding:6px 10px;">+ Add Vehicle</a>')
    if not vehicles:
        return HTMLResponse('<div style="display:flex;gap:8px;align-items:center;">'
                            '<select name="vehicle_id" class="form-select" required>'
                            '<option value="">No vehicles on file — add one first</option></select>'
                            f'{add_link}</div>')
    opts = '<option value="">Select vehicle…</option>'
    for v in vehicles:
        label = f"{v.year} {v.make} {v.model}"
        if v.engine:
            label += f" — {v.engine}"
        if v.license_plate:
            label += f" · {v.license_plate}"
        opts += f'<option value="{v.id}">{escape(label)}</option>'
    return HTMLResponse('<div style="display:flex;gap:8px;align-items:center;">'
                        f'<select name="vehicle_id" class="form-select" required style="flex:1;">{opts}</select>'
                        f'{add_link}</div>')
