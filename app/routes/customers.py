from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Customer, RepairOrder, Vehicle
from app.auth import get_current_user
from app import customer_file, name_finder, quo_import
from app.appointments import PHOENIX
import os, secrets

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
    if type_filter == "fleet":
        query = query.filter(Customer.customer_type == "fleet")
    elif type_filter == "individual":
        query = query.filter(Customer.customer_type == "individual")
    customers = query.order_by(Customer.last_name).all()
    if search.strip():
        customers = [c for c in customers if matches(c, search)]
    return templates.TemplateResponse("customers/list.html", {
        "request": request, "user": user, "customers": customers,
        "search": search, "type_filter": type_filter,
        "names": name_finder.progress(db) if user.role == "owner" else None,
    })


def matches(customer: Customer, search: str) -> bool:
    """Phone in any format (602-555-0100, 6025550100, +1 602…), or every word of a name."""
    digits = "".join(ch for ch in search if ch.isdigit())
    if len(digits) >= 4:
        phone = "".join(ch for ch in (customer.phone or "") if ch.isdigit())
        return digits[-10:] in phone[-10:] or phone[-10:].endswith(digits[-10:])
    text = " ".join(filter(None, (customer.first_name, customer.last_name, customer.fleet_name, customer.email))).lower()
    return all(word in text for word in search.lower().split())


@router.get("/import", response_class=HTMLResponse)
def import_preview(request: Request, db: Session = Depends(get_db)):
    """Owner-only: everyone in Quo who isn't a customer yet. Nothing is saved here."""
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "owner":
        raise HTTPException(status_code=403)
    directory, problem = quo_import.fetch_directory()
    rows = quo_import.preview(db, directory) if directory else []
    return templates.TemplateResponse("customers/import.html", {
        "request": request, "user": user, "rows": rows, "problem": problem, "pretty": quo_import.pretty,
        "complete": (directory or {}).get("complete", True), "done": request.query_params.get("done"),
    })


@router.post("/import")
async def import_run(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "owner":
        raise HTTPException(status_code=403)
    form = await request.form()
    chosen = {p for p in form.getlist("phone") if p.isdigit() and len(p) == 10}
    directory, problem = quo_import.fetch_directory()
    if problem:
        return RedirectResponse("/customers/import", status_code=303)
    made = quo_import.import_rows(db, quo_import.preview(db, directory), chosen, user.username)
    return RedirectResponse(f"/customers/import?done={made}", status_code=303)


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
    vin: str = Form(""),
    year: str = Form(""),
    make: str = Form(""),
    model: str = Form(""),
    concern: str = Form(""),
    then: str = Form("customer"),
):
    """One form: the customer, optionally their vehicle, optionally the job. A known phone opens the existing file."""
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
    if len(digits) == 10:
        for existing in db.query(Customer).filter(Customer.phone.isnot(None)).all():
            if "".join(ch for ch in existing.phone if ch.isdigit())[-10:] == digits:
                return RedirectResponse(f"/customers/{existing.id}?existing=1", status_code=303)

    customer = Customer(
        customer_type=customer_type,
        fleet_name=fleet_name or None,
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        email=email or None,
        phone=phone or None,
        address=address or None,
        notes=notes or None,
        name_source="staff",
    )
    db.add(customer)
    db.flush()
    vehicle = None
    if vin.strip() or make.strip() or model.strip():
        vehicle = Vehicle(customer_id=customer.id, year=int(year) if year.strip().isdigit() else None,
                          make=make.strip() or None, model=model.strip() or None,
                          vin=vin.strip().upper()[:17] or None)
        db.add(vehicle)
        db.flush()
    ro = None
    if then == "job" and vehicle is not None:
        ro = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, concern=concern.strip() or None,
                         public_token=secrets.token_urlsafe(32))
        db.add(ro)
    db.commit()
    if ro is not None:
        return RedirectResponse(f"/ro/{ro.id}", status_code=303)
    if then == "job":
        return RedirectResponse(f"/ro/new?customer_id={customer.id}", status_code=303)
    return RedirectResponse(f"/customers/{customer.id}", status_code=303)


@router.get("/{customer_id}", response_class=HTMLResponse)
def view_customer(request: Request, customer_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404)
    file = customer_file.build(db, customer)
    show = request.query_params.get("show", "all")
    if show not in file["counts"]:
        show = "all"
    return templates.TemplateResponse("customers/detail.html", {
        "request": request, "user": user, "customer": customer, "file": file, "show": show, "tz": PHOENIX,
    })


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
