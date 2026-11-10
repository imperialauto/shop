"""Replacement public website: service pages, online booking and estimate requests.

Lives under /site until the owner approves cutover. Nothing here sends a text,
email or call. Bookings and estimate requests become ordinary customer,
vehicle and repair order records plus a tentative appointment, so staff review
them in the existing Operations pages before anyone contacts the customer.

Online times start at the open times staff publish on /live/control
(booking_slots). A booking reserves the service's full length plus the shop
buffer, or travel time both ways for a mobile visit. A time is only offered and
only confirmed if all of that is free, inside shop hours, under the same
PostgreSQL lock the staff booking API uses. Confirmation appears on screen only.
"""
import logging

from app import business_settings, calendar_feed, estimate_prefill, lead_lifecycle, status_links, website_alerts
import os
import re
import secrets
import hashlib
from dataclasses import dataclass
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.appointment_model import Appointment, BookingSlot
from app.appointments import PHOENIX, AppointmentValidationError, AppointmentWindow, validate_slot
from app.database import get_db
from app.models import Customer, RepairOrder, Vehicle
from app.public_site import (BUSINESS, ROADSIDE_LINES, SERVICE_LINE_BY_SLUG, SERVICE_LINES, VEHICLE_CLASSES, line_actions, FormError, SubmissionLimiter, auto_times, requestable_services,
                             approved_bookable_services, bookable_service, bookable_services, booking_closed_days, clean_text, format_phone,
                             forms_enabled, indexable, mobile_zone, normalize_email, normalize_mileage, normalize_phone,
                             normalize_vin, normalize_year, phone_digits, NAME_LIMIT, TEXT_LIMIT)
from app.routes.appointments import configured_hours, find_conflict, shop_buffer_minutes
from app.routes.live import open_slots, phoenix_label, utc

log = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/site")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
limiter = SubmissionLimiter()

BOOKING_DAYS = 21
WHERE_OPTIONS = OrderedDict([("shop", "At the shop"), ("onsite", "At my location"), ("roadside", "Roadside / broken down")])
CONTACT_OPTIONS = OrderedDict([("text", "Text"), ("call", "Call"), ("email", "Email")])


FAQ = (
    ("Where are you located?", "7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307."),
    ("What are your hours?", "Monday to Friday 11 AM to 7 PM, Saturday 12 PM to 5 PM. Closed Sunday, except qualifying emergency roadside service."),
    ("Can I book online?", "Standard services can be booked online when a time is open. Complex repairs need an estimate and your approval first."),
    ("Do you come to me?", "Selected services can be done at your location, depending on your address. A mobile fee and travel time apply where shown."),
    ("Will I get an estimate before repairs?", "Yes. Complex repairs start with an estimate, and work starts after you approve it."),
    ("How do I check on my vehicle?", "Use the repair status page with your reference number and phone number."),
    ("Do you do tint, wraps and detailing?", "Yes, through NVZN Customs, our appearance division."),
)
STATUS_LIMIT = SubmissionLimiter(limit=20)
RECENT_MINUTES = 30  # the same form sent again in this window returns the first reference


def fingerprint(*parts) -> str:
    return hashlib.sha256("|".join(str(p).strip().lower() for p in parts).encode()).hexdigest()[:24]


def recent_submission(request: Request, key: str) -> dict | None:
    """A double tap, back-and-resend or refresh-resubmit returns the first result instead of a second job."""
    recent = request.session.get("site_recent") or {}
    item = recent.get(key)
    if item and time.time() - item.get("at", 0) < RECENT_MINUTES * 60:
        return item
    return None


def remember_submission(request: Request, key: str, kind: str, item: dict) -> None:
    now = time.time()
    recent = {k: v for k, v in (request.session.get("site_recent") or {}).items() if now - v.get("at", 0) < RECENT_MINUTES * 60}
    recent[key] = {"kind": kind, "item": item, "at": now}
    request.session["site_recent"] = dict(list(recent.items())[-5:])


def lead_minutes() -> int:
    try:
        value = int(business_settings.raw("booking_lead_minutes") or "120")
    except ValueError:
        return 120
    return max(0, min(value, 7 * 24 * 60))


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return "site:" + (forwarded.split(",")[-1].strip() or (request.client.host if request.client else "unknown"))


def page_headers(form_page: bool = False) -> dict:
    headers = {}
    if not indexable():
        headers["X-Robots-Tag"] = "noindex, nofollow"
    if form_page:
        headers["Cache-Control"] = "no-store"
    return headers


def render(request: Request, name: str, status_code: int = 200, form_page: bool = False, **context):
    base = {
        "request": request, "business": BUSINESS, "service_lines": SERVICE_LINES, "line_actions": line_actions,
        "indexable": indexable(), "forms_open": forms_enabled(request.app.state.read_only),
        "year": datetime.now(PHOENIX).year,
    }
    return templates.TemplateResponse(name, {**base, **context}, status_code=status_code,
                                      headers=page_headers(form_page))


# ── Pages ────────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "site/home.html", active="home", bookable=bookable_services())


@router.get("/services", response_class=HTMLResponse)
def services(request: Request):
    return render(request, "site/services.html", active="services")


@router.get("/services/{slug}", response_class=HTMLResponse)
def service_detail(request: Request, slug: str):
    line = SERVICE_LINE_BY_SLUG.get(slug)
    if line is None:
        raise HTTPException(404, "Not found")
    return render(request, "site/service.html", active="services", line=line, actions=line_actions(slug),
                  roadside=slug in ROADSIDE_LINES)


@router.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return render(request, "site/contact.html", active="contact")


@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return render(request, "site/pricing.html", active="pricing", services=approved_bookable_services())


@router.get("/rentals", response_class=HTMLResponse)
def rentals(request: Request):
    return render(request, "site/service.html", active="rentals", line=SERVICE_LINE_BY_SLUG["rentals"],
                  actions=line_actions("rentals"), roadside=False)


@router.get("/faq", response_class=HTMLResponse)
def faq(request: Request):
    return render(request, "site/faq.html", active="faq", questions=FAQ)


def status_page(request: Request, status_code: int = 200, error: str = "", lost: str = ""):
    return render(request, "site/status.html", status_code=status_code, form_page=True, active="status", error=error, lost=lost)


@router.get("/status", response_class=HTMLResponse)
def status_lookup(request: Request):
    return status_page(request)


@router.post("/status", response_class=HTMLResponse)
async def status_find(request: Request, db: Session = Depends(get_db)):
    """Read-only. The same message covers every miss, so it reveals nothing about a record."""
    form = await request.form()
    if not STATUS_LIMIT.allow(client_key(request), time.time()):
        return status_page(request, 429, "Too many tries. Please call or text us.")
    digits = re.sub(r"\D", "", str(form.get("reference", "")))
    try:
        phone = normalize_phone(form.get("phone"))
    except FormError:
        phone = ""
    order = db.get(RepairOrder, int(digits)) if digits and len(digits) <= 9 else None
    if order is None or not order.public_token or not phone or phone_digits(order.customer.phone) != phone:
        return status_page(request, 404, "We could not match that reference and phone number.")
    return RedirectResponse(f"/status/{order.public_token}", status_code=303)


LOST_REPLY = ("Thanks. If that matches a booking or request, someone from the shop will call or text you at that number "
              "during business hours to help you get back to your vehicle status page.")


@router.post("/status/lost", response_class=HTMLResponse)
async def status_lost(request: Request, db: Session = Depends(get_db)):
    """Lost reference. The reply is the same whether or not anything matches, so it reveals nothing.
    A match puts one item in the owner's queue; no booking is created and nothing is sent."""
    if not forms_enabled(request.app.state.read_only):
        return status_page(request, 503, "Please call or text us and we'll find your booking.")
    form = await request.form()
    if not STATUS_LIMIT.allow(client_key(request), time.time()):
        return status_page(request, 429, "Too many tries. Please call or text us.")
    try:
        phone = normalize_phone(form.get("phone"))
    except FormError:
        phone = ""
    if phone:
        eligible = (db.query(Customer).join(RepairOrder, RepairOrder.customer_id == Customer.id)
                    .filter(Customer.phone.isnot(None), RepairOrder.public_token.isnot(None),
                            RepairOrder.status != "delivered").distinct())
        matches = []
        for customer in eligible.yield_per(200):
            if phone_digits(customer.phone) == phone:
                matches.append(customer)
                if len(matches) == 2:
                    break
        if len(matches) == 1:
            customer = matches[0]
            orders = (db.query(RepairOrder).filter(RepairOrder.customer_id == customer.id,
                                                   RepairOrder.public_token.isnot(None),
                                                   RepairOrder.status != "delivered")
                      .order_by(RepairOrder.id.desc()).limit(5).all())
            if orders:
                website_alerts.queue_reference_help(db, customer, orders)
                db.commit()
    return status_page(request, 200, lost=LOST_REPLY)


@router.get("/status/recover/{ticket}", response_class=HTMLResponse)
def status_recover(request: Request, ticket: str, db: Session = Depends(get_db)):
    order = status_links.resolve(db, ticket)
    if order is None:
        return status_page(request, 404, "This status link expired or is unavailable. Ask the shop for a new link.")
    return RedirectResponse(f"/status/{order.public_token}", status_code=303,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# ── Shared record creation ───────────────────────────────────────────────────
def person_fields(form) -> dict:
    first = clean_text(form.get("first_name"), NAME_LIMIT)
    last = clean_text(form.get("last_name"), NAME_LIMIT)
    if not first or not last:
        raise FormError("Enter your first and last name.")
    contact = form.get("contact_method", "text")
    if contact not in CONTACT_OPTIONS:
        raise FormError("Choose how we should reach you.")
    email = normalize_email(form.get("email"))
    if contact == "email" and not email:
        raise FormError("Enter an email address, or choose text or call.")
    return {"first": first, "last": last, "phone": normalize_phone(form.get("phone")), "email": email, "contact": contact}


def vehicle_fields(form, require_make: bool = True) -> dict:
    make = clean_text(form.get("make"), 64)
    model = clean_text(form.get("model"), 128)
    if require_make and (not make or not model):
        raise FormError("Enter the vehicle make and model.")
    return {
        "year": normalize_year(form.get("year"), datetime.now(PHOENIX).year),
        "make": make, "model": model,
        "vin": normalize_vin(form.get("vin")),
        "mileage": normalize_mileage(form.get("mileage")),
    }


def find_or_create_customer(db: Session, person: dict) -> tuple[Customer, bool]:
    """Reuses a record only when both phone and last name match, so one customer keeps one record.
    The website never shows what is on file, so a match reveals nothing to the visitor."""
    same_name = db.query(Customer).filter(func.lower(Customer.last_name) == person["last"].lower()).limit(200).all()
    match = next((c for c in same_name if phone_digits(c.phone) == person["phone"]), None)
    if match is not None:
        if person["email"] and not match.email:
            match.email = person["email"]
        return match, True
    customer = Customer(
        customer_type="individual", first_name=person["first"], last_name=person["last"],
        phone=format_phone(person["phone"]), email=person["email"] or None,
        notes=f"Created from the public website on {datetime.now(PHOENIX):%Y-%m-%d}. Not yet confirmed by staff.",
    )
    db.add(customer)
    db.flush()
    return customer, False


def find_or_create_vehicle(db: Session, customer: Customer, matched: bool, details: dict) -> Vehicle:
    if matched and details["vin"]:
        existing = db.query(Vehicle).filter(Vehicle.customer_id == customer.id, Vehicle.vin == details["vin"]).first()
        if existing is not None:
            return existing
    vehicle = Vehicle(customer_id=customer.id, year=details["year"], make=details["make"] or "Unknown",
                      model=details["model"] or "Unknown", vin=details["vin"] or None, mileage=details["mileage"],
                      notes="Entered by the customer on the public website.")
    db.add(vehicle)
    db.flush()
    return vehicle


def concern_text(heading: str, person: dict, matched: bool, lines: list[str]) -> str:
    parts = [heading, *[line for line in lines if line],
             f"Preferred contact: {CONTACT_OPTIONS[person['contact']]}.",
             "Submitted on the public website. No message has been sent to the customer."]
    if matched:
        parts.append("Matched an existing customer by phone and last name. Confirm before contacting.")
    return "\n".join(parts)[:4000]


def new_repair_order(db: Session, customer: Customer, vehicle: Vehicle, concern: str, mileage: int | None) -> RepairOrder:
    order = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, status="intake", concern=concern,
                        mileage_in=mileage, public_token=secrets.token_urlsafe(32))
    db.add(order)
    db.flush()
    return order


def form_values(form) -> dict:
    return {key: str(value)[:TEXT_LIMIT] for key, value in form.items()}


# ── Online booking ───────────────────────────────────────────────────────────
def package_price(service, vehicle_class: str = "", axles: int = 1) -> float | None:
    """Starting price for a package: the vehicle-class tier times the axles. Plain services use their price."""
    if service.tiers:
        tier = service.tier(vehicle_class)
        return None if tier is None else tier[2] * max(1, min(axles, 2))
    return service.price


def total_price(service, zone, vehicle_class: str = "", axles: int = 1) -> float | None:
    price = package_price(service, vehicle_class, axles)
    if price is None or (zone and zone.fee is None):
        return None
    return price + (zone.fee if zone else 0)


@dataclass(frozen=True)
class Opening:
    """A time a customer can pick: a published slot, or one generated from shop hours."""
    id: str
    resource_id: str
    starts_at: datetime


def generated_openings(now: datetime) -> list[Opening]:
    setup = auto_times()
    if not setup:
        return []
    try:
        hours = configured_hours()
    except HTTPException:
        return []
    found = []
    today = now.astimezone(PHOENIX).date()
    for offset in range(BOOKING_DAYS + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in hours:
            continue
        opens, closes = hours[day.weekday()]
        start = datetime.combine(day, opens, tzinfo=PHOENIX)
        end = datetime.combine(day, closes, tzinfo=PHOENIX)
        while start < end:
            for index, resource in enumerate(setup["resources"]):
                found.append(Opening(f"a{index}-{int(start.timestamp())}", resource, start.astimezone(timezone.utc)))
            start += timedelta(minutes=setup["step_minutes"])
    return found


def openings(db: Session, now: datetime) -> list[Opening]:
    published = [Opening(str(s.id), s.resource_id, utc(s.starts_at)) for s in open_slots(db, now)]
    taken = {(o.resource_id, o.starts_at) for o in published}
    extra = [o for o in generated_openings(now) if (o.resource_id, o.starts_at) not in taken]
    return sorted(published + extra, key=lambda o: (o.starts_at, o.resource_id))


def resolve_opening(db: Session, slot_id: str, lock: bool = False) -> Opening | None:
    """The chosen time, re-checked on the server. Generated times must still line up with today's settings."""
    slot_id = (slot_id or "").strip()
    if slot_id.startswith("a"):
        setup = auto_times()
        try:
            index, stamp = slot_id[1:].split("-", 1)
            resource = setup["resources"][int(index)]
            start = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
        except (KeyError, ValueError, IndexError, TypeError):
            return None
        local = start.astimezone(PHOENIX)
        if local.second or (local.hour * 60 + local.minute) % setup["step_minutes"]:
            return None
        if lock:
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:resource))"), {"resource": resource})
        return Opening(slot_id, resource, start)
    if not slot_id.isdigit():
        return None
    slot = db.get(BookingSlot, int(slot_id))
    if slot is None or slot.removed_at is not None:
        return None
    if lock:
        # Same transaction-scoped lock as the staff booking API, released on commit or rollback.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:resource))"), {"resource": slot.resource_id})
        db.refresh(slot)
        if slot.removed_at is not None:
            return None
    return Opening(str(slot.id), slot.resource_id, utc(slot.starts_at))


def booking_window(slot: BookingSlot, service, zone) -> AppointmentWindow:
    """The full time this booking holds. A mobile visit holds travel both ways instead of the shop buffer."""
    start = utc(slot.starts_at)
    travel = zone.travel_minutes if zone else 0
    return AppointmentWindow(start, start + timedelta(minutes=service.minutes), slot.resource_id, travel,
                             travel if zone else shop_buffer_minutes())


def calendar_busy(now: datetime) -> list[dict] | None:
    """Events on the owner's linked calendar for the booking window. [] when no calendar is linked;
    None when one is linked but can't be read, and then nothing is offered (no double booking)."""
    if not calendar_feed.feed_urls():
        return []
    return calendar_feed.upcoming(now, days=BOOKING_DAYS + 2)


def fits(db: Session, window: AppointmentWindow, busy: list[dict] | None = ()) -> bool:
    if window.starts_at.astimezone(PHOENIX).weekday() in booking_closed_days():
        return False
    if busy is None or calendar_feed.busy_overlaps(busy, window.blocked_from, window.blocked_until):
        return False
    try:  # the buffer or trip back must also end inside open hours
        held = AppointmentWindow(window.starts_at, window.blocked_until, window.resource_id)
        validate_slot(held, [], configured_hours())
    except AppointmentValidationError:
        return False
    return find_conflict(db, window) is None


def booking_times(db: Session, service, zone) -> "OrderedDict[str, list[dict]]":
    now = datetime.now(timezone.utc)
    earliest = now + timedelta(minutes=lead_minutes())
    latest = now + timedelta(days=BOOKING_DAYS)
    days: OrderedDict[str, list[dict]] = OrderedDict()
    busy = calendar_busy(now)
    for slot in openings(db, now):
        start = slot.starts_at
        if not earliest <= start <= latest or not fits(db, booking_window(slot, service, zone), busy):
            continue
        local = start.astimezone(PHOENIX)
        hour = local.hour % 12 or 12
        days.setdefault(f"{local:%A}, {local:%B} {local.day}", []).append(
            {"id": slot.id, "label": f"{hour}:{local:%M} {local:%p}"})
    return days


def booking_unavailable_reason(db: Session) -> str:
    """Empty when online booking can take a booking right now."""
    try:
        hours = configured_hours()
    except HTTPException:
        return "Shop hours are not set up for online booking."
    if not hours:
        return "Shop hours are not set up for online booking."
    if db.bind.dialect.name != "postgresql":
        return "Online booking needs the PostgreSQL database."
    if calendar_busy(datetime.now(timezone.utc)) is None:
        return "The shop calendar can't be checked right now."
    return ""


def booking_page(request: Request, db: Session, status_code: int = 200, error: str = "", values: dict | None = None):
    values = values or {}
    query = request.query_params
    service = bookable_service(values.get("service") or query.get("service", ""))
    reason = booking_unavailable_reason(db)
    where = values.get("where") or query.get("where", "shop")
    where = "mobile" if where == "mobile" and service and service.mobile else "shop"
    zip_code = clean_text(values.get("zip") or query.get("zip"), 5)
    zone = mobile_zone(zip_code) if where == "mobile" else None
    ready = bool(service) and not reason and (where == "shop" or zone is not None)
    vehicle_class = values.get("vehicle_class") or query.get("vehicle_class", "")
    axles = 2 if (values.get("axles") or query.get("axles")) == "2" else 1
    if service and service.tiers and not service.tier(vehicle_class):
        vehicle_class = ""
    total = total_price(service, zone, vehicle_class, axles) if service else None
    return render(request, "site/book.html", status_code=status_code, form_page=True, active="book",
                  services=bookable_services(), requestable=requestable_services(), vehicle_class=vehicle_class, axles=axles,
                  service=service, days=booking_times(db, service, zone) if ready else {},
                  where=where, zip_code=zip_code, zone=zone, total=total, ready=ready,
                  unavailable=reason, error=error, values=values, contact_options=CONTACT_OPTIONS)


@router.get("/book", response_class=HTMLResponse)
def book(request: Request, db: Session = Depends(get_db)):
    return booking_page(request, db)


@router.post("/book", response_class=HTMLResponse)
async def book_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = form_values(form)
    if not forms_enabled(request.app.state.read_only):
        return booking_page(request, db, 503, "Online booking is not open yet. Please call or text us.", values)
    if values.get("company"):  # hidden field only automated spam fills in
        request.session["site_booking"] = {"ref": "", "service": "", "time": ""}
        return RedirectResponse("/site/book/received", status_code=303)
    reason = booking_unavailable_reason(db)
    if reason:
        return booking_page(request, db, 503, "Online booking is not available right now. Please call or text us.", values)
    try:
        service = bookable_service(values.get("service", ""))
        if service is None:
            raise FormError("Choose a service.")
        person = person_fields(form)
        vehicle_details = vehicle_fields(form)
        notes = clean_text(form.get("notes"), TEXT_LIMIT)
        where = values.get("where", "shop")
        zone, street = None, ""
        if where == "mobile":
            zone = mobile_zone(values.get("zip")) if service.mobile else None
            street = clean_text(form.get("street"), 200)
            if zone is None:
                raise FormError("We can't book that address online. Please send an estimate request.")
            if not street:
                raise FormError("Enter the street address for the visit.")
        elif where != "shop":
            raise FormError("Choose the shop or your location.")
        slot_id = values.get("slot_id", "").strip()
        if not slot_id:
            raise FormError("Choose a time.")
        vehicle_class, axles = values.get("vehicle_class", ""), 2 if values.get("axles") == "2" else 1
        if service.tiers and not service.tier(vehicle_class):
            raise FormError("Choose your type of vehicle.")
    except FormError as exc:
        log.info("site_form_rejected form=booking reason=%s", str(exc)[:60])  # the message only, never what was typed
        return booking_page(request, db, 422, str(exc), values)
    resend = fingerprint("book", person["phone"], service.slug if hasattr(service, "slug") else service.name, slot_id)
    if recent_submission(request, resend):
        request.session["site_booking"] = recent_submission(request, resend)["item"]
        return RedirectResponse("/site/book/received", status_code=303)
    # Counted after validation so a typo does not use up a visitor's tries.
    if not limiter.allow(client_key(request), time.time()):
        return booking_page(request, db, 429, "Too many requests from this connection. Please call or text us.", values)

    taken = "That time was just taken. Please choose another."
    try:
        slot = resolve_opening(db, slot_id, lock=True)
        if slot is None:
            raise FormError(taken)
        starts_at = slot.starts_at
        if starts_at < datetime.now(timezone.utc) + timedelta(minutes=lead_minutes()) \
                or starts_at > datetime.now(timezone.utc) + timedelta(days=BOOKING_DAYS + 1):
            raise FormError(taken)
        candidate = booking_window(slot, service, zone)
        if not fits(db, candidate, calendar_busy(datetime.now(timezone.utc))):
            raise FormError(taken)
        customer, matched = find_or_create_customer(db, person)
        vehicle = find_or_create_vehicle(db, customer, matched, vehicle_details)
        label = phoenix_label(starts_at)
        total = total_price(service, zone, vehicle_class, axles)
        package = ""
        if service.tiers:
            package = (f"Package: {VEHICLE_CLASSES[vehicle_class]}, {axles} axle{'s' if axles == 2 else ''}. "
                       f"Starting price shown ${package_price(service, vehicle_class, axles):,.2f}. Confirm VIN parts fitment, "
                       "vendor cost, labor time and margin, then confirm the final price with the customer before work.")
        concern = concern_text(f"Online booking: {service.name} on {label}.", person, matched, [
            package,
            f"Mobile visit: {street}, {values.get('zip')}. Travel {zone.travel_minutes} minutes each way." if zone else "At the shop.",
            f"Price shown: {service.price_label}." + (f" {'Starting total' if service.price_type == 'starting' else 'Total'} shown: ${total:,.2f}." if total is not None else " No final price yet."),
            f"Customer notes: {notes}" if notes else "",
            f"Mileage given: {vehicle_details['mileage']:,}" if vehicle_details["mileage"] else "",
        ])
        order = new_repair_order(db, customer, vehicle, concern, vehicle_details["mileage"])
        appointment = Appointment(repair_order_id=order.id, resource_id=slot.resource_id, starts_at=starts_at, ends_at=candidate.ends_at,
                           status="confirmed", booked_value=total or 0, location="mobile" if zone else "shop",
                           travel_before_minutes=candidate.travel_before_minutes, buffer_after_minutes=candidate.buffer_after_minutes)
        db.add(appointment)
        db.flush()
        item = website_alerts.queue(db, order, "booking", f"Booked {service.name}, {label}{', mobile visit' if zone else ''}."
                                    + (" Starting price: check parts for this VIN and confirm the final price." if service.price_type == "starting" else ""),
                                    vehicle_text=" ".join(str(p) for p in (vehicle_details["year"], vehicle_details["make"], vehicle_details["model"]) if p),
                                    review=service.price_type == "starting")
        lead_lifecycle.intake_from_website(db, order, item, appointment)
        db.commit()
    except FormError as exc:
        db.rollback()
        return booking_page(request, db, 409, str(exc), {**values, "slot_id": ""})
    except Exception:
        db.rollback()
        raise
    shown = package_price(service, vehicle_class, axles)
    price_text = service.price_label if not service.tiers else (
        f"From ${shown:,.2f} for {axles} axle{'s' if axles == 2 else ''} ({VEHICLE_CLASSES[vehicle_class].lower()})".replace(".00", ""))
    request.session["site_booking"] = {"ref": f"WEB-{order.id}", "service": service.name, "time": label,
                                       "price": price_text, "starting": service.price_type == "starting",
                                       "fee": (zone.fee or 0) if zone else 0, "token": order.public_token}
    remember_submission(request, resend, "booking", request.session["site_booking"])
    return RedirectResponse("/site/book/received", status_code=303)


@router.get("/book/received", response_class=HTMLResponse)
def book_received(request: Request):
    # Kept (not popped) so a refresh still shows the reference in this browser.
    booking = request.session.get("site_booking")
    if booking is None:
        return RedirectResponse("/site/book", status_code=303)
    return render(request, "site/received.html", form_page=True, active="book", kind="booking", item=booking)


# ── Estimate requests ────────────────────────────────────────────────────────
def estimate_page(request: Request, status_code: int = 200, error: str = "", values: dict | None = None):
    values = values or {}
    if not values.get("service_line") and request.query_params.get("service") in SERVICE_LINE_BY_SLUG:
        values["service_line"] = request.query_params["service"]
    # Coming from a standard service without a set price: carry the choice over so nothing is typed twice.
    about = next((s for s in approved_bookable_services() if s.slug == request.query_params.get("about")), None)
    if about and not values.get("description"):
        values["description"] = f"I'd like to book: {about.name}. "
        values.setdefault("service_line", "auto-repair")
        values["about"] = about.slug
    return render(request, "site/estimate.html", status_code=status_code, form_page=True, active="estimate",
                  error=error, values=values, where_options=WHERE_OPTIONS, contact_options=CONTACT_OPTIONS)


@router.get("/estimate", response_class=HTMLResponse)
def estimate(request: Request):
    return estimate_page(request)


@router.post("/estimate", response_class=HTMLResponse)
async def estimate_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    values = form_values(form)
    if not forms_enabled(request.app.state.read_only):
        return estimate_page(request, 503, "Estimate requests are not open online yet. Please call or text us.", values)
    if values.get("company"):
        request.session["site_estimate"] = {"ref": ""}
        return RedirectResponse("/site/estimate/received", status_code=303)
    try:
        person = person_fields(form)
        details = vehicle_fields(form, require_make=not normalize_vin(form.get("vin")))
        line = SERVICE_LINE_BY_SLUG.get(values.get("service_line", ""))
        where = values.get("where", "shop")
        if where not in WHERE_OPTIONS:
            raise FormError("Choose where the vehicle is.")
        description = clean_text(form.get("description"), TEXT_LIMIT)
        if len(description) < 10:
            raise FormError("Tell us what is going on with the vehicle.")
        location = clean_text(form.get("location"), 256)
        if where != "shop" and not location:
            raise FormError("Tell us where the vehicle is.")
    except FormError as exc:
        log.info("site_form_rejected form=estimate reason=%s", str(exc)[:60])
        return estimate_page(request, 422, str(exc), values)
    return save_request(request, db, person, details, line, where, location, description,
                        retry=lambda code, message: estimate_page(request, code, message, values))


def save_request(request: Request, db: Session, person: dict, details: dict, line, where: str, location: str,
                 description: str, retry, heading: str = "Estimate request from the website."):
    """One request makes one customer, vehicle, job, lead and owner item, with the estimate started
    from approved prices. Shared by estimate and roadside requests."""
    resend = fingerprint("estimate", person["phone"], " ".join(description.split()))
    if recent_submission(request, resend):
        request.session["site_estimate"] = recent_submission(request, resend)["item"]
        return RedirectResponse("/site/estimate/received", status_code=303)
    if not limiter.allow(client_key(request), time.time()):
        return retry(429, "Too many requests from this connection. Please call or text us.")
    try:
        customer, matched = find_or_create_customer(db, person)
        vehicle = find_or_create_vehicle(db, customer, matched, details)
        concern = concern_text(heading, person, matched, [
            f"Service: {line.name}." if line else "",
            f"Where: {WHERE_OPTIONS[where]}{' — ' + location if location else ''}.",
            f"Mileage given: {details['mileage']:,}" if details["mileage"] else "",
            f"Details: {description}",
        ])
        order = new_repair_order(db, customer, vehicle, concern, details["mileage"])
        started = estimate_prefill.prefill(db, order, line.slug if line else "", where)
        order.concern = (order.concern + "\n" + "Estimate started: " + started)[:4000]
        db.flush()
        item = website_alerts.queue(db, order, "request", f"{line.name if line else 'Estimate'}: {WHERE_OPTIONS[where]}. {description}"[:200]
                                    + f" | {started}",
                                    service_line=line.slug if line else "",
                                    vehicle_text=" ".join(str(p) for p in (details["year"], details["make"], details["model"]) if p))
        lead_lifecycle.intake_from_website(db, order, item)
        db.commit()
    except Exception:
        db.rollback()
        raise
    request.session["site_estimate"] = {"ref": f"WEB-{order.id}", "token": order.public_token,
                                        "roadside": where == "roadside"}
    remember_submission(request, resend, "estimate", request.session["site_estimate"])
    return RedirectResponse("/site/estimate/received", status_code=303)


ROADSIDE_SITUATIONS = OrderedDict([
    ("tow", "Needs a tow"), ("no-start", "Won't start or dead battery"), ("flat", "Flat tire"),
    ("overheat", "Overheating or leaking"), ("accident", "Accident, no one hurt"), ("other", "Something else"),
])


def roadside_page(request: Request, status_code: int = 200, error: str = "", values: dict | None = None):
    return render(request, "site/roadside.html", status_code=status_code, form_page=True, active="roadside",
                  error=error, values=values or {}, situations=ROADSIDE_SITUATIONS, contact_options=CONTACT_OPTIONS)


@router.get("/roadside", response_class=HTMLResponse)
def roadside(request: Request):
    return roadside_page(request)


@router.post("/roadside", response_class=HTMLResponse)
async def roadside_submit(request: Request, db: Session = Depends(get_db)):
    """A written roadside or tow request. It is never dispatched automatically: the owner confirms it first."""
    form = await request.form()
    values = form_values(form)
    if not forms_enabled(request.app.state.read_only):
        return roadside_page(request, 503, "Online roadside requests are not open yet. Please call us.", values)
    if values.get("company"):
        request.session["site_estimate"] = {"ref": ""}
        return RedirectResponse("/site/estimate/received", status_code=303)
    try:
        situation = values.get("situation", "")
        if situation not in ROADSIDE_SITUATIONS:
            raise FormError("Choose what's going on.")
        location = clean_text(form.get("location"), 256)
        if len(location) < 5:
            raise FormError("Tell us where the vehicle is: an address, cross streets or a mile marker.")
        person = person_fields(form)
        details = vehicle_fields(form, require_make=not normalize_vin(form.get("vin")))
        extra = clean_text(form.get("description"), TEXT_LIMIT)
    except FormError as exc:
        log.info("site_form_rejected form=roadside reason=%s", str(exc)[:60])
        return roadside_page(request, 422, str(exc), values)
    line = SERVICE_LINE_BY_SLUG["towing" if situation in ("tow", "accident") else "mobile-roadside"]
    description = f"Roadside: {ROADSIDE_SITUATIONS[situation]}." + (f" {extra}" if extra else "")
    return save_request(request, db, person, details, line, "roadside", location, description,
                        retry=lambda code, message: roadside_page(request, code, message, values),
                        heading="Roadside request from the website. Not dispatched: confirm with the customer first.")


@router.get("/estimate/received", response_class=HTMLResponse)
def estimate_received(request: Request):
    item = request.session.get("site_estimate")
    if item is None:
        return RedirectResponse("/site/estimate", status_code=303)
    return render(request, "site/received.html", form_page=True, active="estimate", kind="estimate", item=item)
