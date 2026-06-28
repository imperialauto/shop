from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Customer
from app.auth import get_current_user
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))


def require_user(request: Request, db: Session):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


@router.get("/", response_class=HTMLResponse)
def list_customers(request: Request, db: Session = Depends(get_db), search: str = "", type_filter: str = "all"):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    query = db.query(Customer)
    if search:
        query = query.filter(
            (Customer.first_name.ilike(f"%{search}%")) |
            (Customer.last_name.ilike(f"%{search}%")) |
            (Customer.fleet_name.ilike(f"%{search}%")) |
            (Customer.phone.ilike(f"%{search}%"))
        )
    if type_filter == "fleet":
        query = query.filter(Customer.customer_type == "fleet")
    elif type_filter == "individual":
        query = query.filter(Customer.customer_type == "individual")

    customers = query.order_by(Customer.last_name).all()
    return templates.TemplateResponse("customers/list.html", {
        "request": request, "user": user, "customers": customers,
        "search": search, "type_filter": type_filter
    })


@router.get("/new", response_class=HTMLResponse)
def new_customer_form(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("customers/form.html", {"request": request, "user": user, "customer": None})


@router.post("/new")
def create_customer(
    request: Request,
    db: Session = Depends(get_db),
    customer_type: str = Form("individual"),
    fleet_name: str = Form(""),
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    customer = Customer(
        customer_type=customer_type,
        fleet_name=fleet_name or None,
        first_name=first_name,
        last_name=last_name,
        email=email or None,
        phone=phone or None,
        address=address or None,
        notes=notes or None,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return RedirectResponse(f"/customers/{customer.id}", status_code=302)


@router.get("/{customer_id}", response_class=HTMLResponse)
def view_customer(request: Request, customer_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("customers/detail.html", {"request": request, "user": user, "customer": customer})


@router.get("/{customer_id}/edit", response_class=HTMLResponse)
def edit_customer_form(request: Request, customer_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("customers/form.html", {"request": request, "user": user, "customer": customer})


@router.post("/{customer_id}/edit")
def update_customer(
    request: Request,
    customer_id: int,
    db: Session = Depends(get_db),
    customer_type: str = Form("individual"),
    fleet_name: str = Form(""),
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    notes: str = Form(""),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404)

    customer.customer_type = customer_type
    customer.fleet_name = fleet_name or None
    customer.first_name = first_name
    customer.last_name = last_name
    customer.email = email or None
    customer.phone = phone or None
    customer.address = address or None
    customer.notes = notes or None
    db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=302)
