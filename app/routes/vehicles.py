from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Vehicle, Customer
from app.auth import get_current_user
import os

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

COMMON_MAKES = ["Ford", "Chevrolet", "GMC", "Dodge", "Ram", "Mercedes-Benz", "Sprinter",
                "Toyota", "Honda", "Nissan", "Jeep", "Chrysler", "Volkswagen", "BMW"]

DIESEL_MODELS = {
    "Ford": ["F-250 Super Duty", "F-350 Super Duty", "F-450 Super Duty", "F-550 Super Duty", "Transit"],
    "Chevrolet": ["Silverado 2500HD", "Silverado 3500HD", "Colorado"],
    "GMC": ["Sierra 2500HD", "Sierra 3500HD", "Canyon"],
    "Dodge": ["Ram 2500", "Ram 3500"],
    "Ram": ["2500", "3500", "ProMaster"],
    "Mercedes-Benz": ["Sprinter 2500", "Sprinter 3500", "Metris"],
}


def require_user(request, db):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


@router.get("/new", response_class=HTMLResponse)
def new_vehicle_form(request: Request, customer_id: int = None, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customers = db.query(Customer).order_by(Customer.last_name).all()
    return templates.TemplateResponse("vehicles/form.html", {
        "request": request, "user": user, "vehicle": None,
        "customers": customers, "selected_customer_id": customer_id,
        "makes": COMMON_MAKES, "diesel_models": DIESEL_MODELS,
    })


@router.post("/new")
def create_vehicle(
    request: Request,
    db: Session = Depends(get_db),
    customer_id: int = Form(...),
    year: int = Form(None),
    make: str = Form(""),
    model: str = Form(""),
    trim: str = Form(""),
    engine: str = Form(""),
    vin: str = Form(""),
    license_plate: str = Form(""),
    mileage: int = Form(None),
    color: str = Form(""),
    notes: str = Form(""),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    vehicle = Vehicle(
        customer_id=customer_id, year=year, make=make or None,
        model=model or None, trim=trim or None, engine=engine or None,
        vin=vin or None, license_plate=license_plate or None,
        mileage=mileage, color=color or None, notes=notes or None,
    )
    db.add(vehicle)
    db.commit()
    db.refresh(vehicle)
    return RedirectResponse(f"/customers/{customer_id}", status_code=302)


@router.get("/{vehicle_id}/edit", response_class=HTMLResponse)
def edit_vehicle_form(request: Request, vehicle_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404)
    customers = db.query(Customer).order_by(Customer.last_name).all()
    return templates.TemplateResponse("vehicles/form.html", {
        "request": request, "user": user, "vehicle": vehicle,
        "customers": customers, "selected_customer_id": vehicle.customer_id,
        "makes": COMMON_MAKES, "diesel_models": DIESEL_MODELS,
    })


@router.get("/{vehicle_id}", response_class=HTMLResponse)
def vehicle_detail(request: Request, vehicle_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404)
    # Sort ROs oldest→newest for timeline
    ros = sorted(vehicle.repair_orders, key=lambda r: r.created_at)
    return templates.TemplateResponse("vehicles/detail.html", {
        "request": request, "user": user, "vehicle": vehicle, "ros": ros,
    })


@router.post("/{vehicle_id}/edit")
def update_vehicle(
    request: Request,
    vehicle_id: int,
    db: Session = Depends(get_db),
    customer_id: int = Form(...),
    year: int = Form(None),
    make: str = Form(""),
    model: str = Form(""),
    trim: str = Form(""),
    engine: str = Form(""),
    vin: str = Form(""),
    license_plate: str = Form(""),
    mileage: int = Form(None),
    color: str = Form(""),
    notes: str = Form(""),
):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404)

    vehicle.customer_id = customer_id
    vehicle.year = year
    vehicle.make = make or None
    vehicle.model = model or None
    vehicle.trim = trim or None
    vehicle.engine = engine or None
    vehicle.vin = vin or None
    vehicle.license_plate = license_plate or None
    vehicle.mileage = mileage
    vehicle.color = color or None
    vehicle.notes = notes or None
    db.commit()
    return RedirectResponse(f"/customers/{customer_id}", status_code=302)
