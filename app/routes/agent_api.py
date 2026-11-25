"""Assistant API: what an owner-issued assistant key (app/agent_keys.py) may do.

Mounted at /internal/agent/v1. Send the key as `Authorization: Bearer iak_...`
(or `X-Agent-Key: iak_...`). The key never opens a staff page or a session.

Deliberately limited:
* customers, vehicles, jobs/estimate lines and appointments only (confirming one
  needs its own scope, for when the owner says yes);
* never invoices, payments, finance, logins, settings or messaging;
* nothing here texts or emails a customer or changes a price that a customer has
  already been asked to approve or that is invoiced, except one thing behind its own
  scope (estimates:send): texting a job's approval link, through the same send as the
  owner's Ask for approval button, so it is logged on the job and never sent twice;
* a job's status can be moved on the board, but never to Complete or Picked up,
  because those can text the customer (pickup text, review request);
* every change is written to agent_actions in the same transaction.
Read-only (recovery) mode still blocks every write, as for staff.
"""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import agent_keys, next_action, ro_status_policy, website_alerts
from app.agent_keys import AgentKey
from app.appointment_model import Appointment
from app.database import get_db
from app.models import Customer, LineItem, RepairOrder, Vehicle
from app.routes.account_security import client_key
from app.routes.appointments import AppointmentInput, close, confirm, create_appointment
from app.security import limiter

router = APIRouter()


def _presented(authorization: str | None, x_agent_key: str | None) -> str | None:
    if x_agent_key:
        return x_agent_key.strip()
    if authorization and authorization[:7].lower() == "bearer ":
        return authorization[7:].strip()
    return None


def agent(scope: str):
    """Dependency: the active key, which must carry `scope`."""
    if scope not in agent_keys.SCOPES:
        raise ValueError(scope)

    def dependency(request: Request, db: Session = Depends(get_db),
                   authorization: str | None = Header(default=None),
                   x_agent_key: str | None = Header(default=None)) -> AgentKey:
        if not agent_keys.enabled():
            raise HTTPException(503, "The assistant API is turned off.")
        throttle = "agent:" + client_key(request)
        if limiter.locked(throttle):
            raise HTTPException(429, "Too many failed key attempts. Wait a few minutes.")
        key = agent_keys.find(db, _presented(authorization, x_agent_key))
        if key is None:
            limiter.record(throttle)
            raise HTTPException(401, "Missing, expired or revoked assistant key.")
        if scope not in key.scope_list:
            raise HTTPException(403, f"This key is not allowed to {agent_keys.SCOPES[scope].lower()}.")
        key.last_used_at = agent_keys.utc_now()
        db.commit()
        return key

    return dependency


# ---------------------------------------------------------------- serializers

def _digits(phone: str | None) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())[-10:]


def vehicle_json(v: Vehicle) -> dict:
    return {"id": v.id, "customer_id": v.customer_id, "year": v.year, "make": v.make, "model": v.model,
            "trim": v.trim, "engine": v.engine, "vin": v.vin, "license_plate": v.license_plate,
            "mileage": v.mileage, "color": v.color, "notes": v.notes}


def customer_json(c: Customer, detail: bool = False) -> dict:
    data = {"id": c.id, "first_name": c.first_name, "last_name": c.last_name, "phone": c.phone,
            "email": c.email, "customer_type": c.customer_type, "fleet_name": c.fleet_name}
    if detail:
        data.update(address=c.address, notes=c.notes, vehicles=[vehicle_json(v) for v in c.vehicles],
                    jobs=[{"id": r.id, "vehicle_id": r.vehicle_id, "status": r.status, "concern": r.concern}
                          for r in sorted(c.repair_orders, key=lambda r: r.id, reverse=True)[:20]])
    return data


def line_json(li: LineItem) -> dict:
    return {"id": li.id, "description": li.description, "item_type": li.item_type, "quantity": li.quantity,
            "unit_price": li.unit_price, "taxable": li.taxable, "approval": li.approval, "total": li.total}


def job_json(ro: RepairOrder) -> dict:
    return {"id": ro.id, "customer_id": ro.customer_id, "vehicle_id": ro.vehicle_id, "status": ro.status,
            "concern": ro.concern, "tech_notes": ro.tech_notes, "mileage_in": ro.mileage_in,
            "invoiced": bool(ro.invoices), "lines": [line_json(li) for li in ro.line_items]}


# ---------------------------------------------------------------- inputs

def _blank_to_none(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class CustomerIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=64)
    last_name: str = Field(min_length=1, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=128)
    address: str | None = Field(default=None, max_length=256)
    notes: str | None = Field(default=None, max_length=4000)
    customer_type: str = Field(default="individual", pattern="^(individual|fleet)$")
    fleet_name: str | None = Field(default=None, max_length=128)

    _clean = field_validator("phone", "email", "address", "notes", "fleet_name", mode="before")(_blank_to_none)


class VehicleIn(BaseModel):
    year: int | None = Field(default=None, ge=1900, le=2100)
    make: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=128)
    trim: str | None = Field(default=None, max_length=64)
    engine: str | None = Field(default=None, max_length=64)
    vin: str | None = Field(default=None, max_length=17)
    license_plate: str | None = Field(default=None, max_length=16)
    mileage: int | None = Field(default=None, ge=0, le=5_000_000)
    color: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=4000)

    _clean = field_validator("make", "model", "trim", "engine", "vin", "license_plate", "color", "notes",
                             mode="before")(_blank_to_none)

    @field_validator("vin")
    @classmethod
    def upper_vin(cls, value):
        return value.upper() if value else value


class LineIn(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    item_type: str = Field(default="labor", pattern="^(labor|part|sublet|misc|discount)$",
                           description="labor, part, sublet, misc, or discount. A discount is money off the job: "
                           "give unit_price as the positive amount off (75 for $75 off). It is saved as a "
                           "negative, untaxed line the shop has already approved, so the customer sees it as "
                           "money off on the approval page and invoice.")
    quantity: float = Field(default=1.0, gt=0, le=10_000)
    unit_price: float = Field(default=0.0, ge=0, le=1_000_000)
    taxable: bool = True


def _add_line(db: Session, ro: RepairOrder, line: LineIn, actor: str) -> None:
    if line.item_type == "discount":
        from app import discounts
        try:
            discounts.add(db, ro, line.description, line.unit_price * line.quantity, actor)
        except discounts.DiscountError as exc:
            if str(exc) == "too_big":
                raise HTTPException(422, "The discounts would be more than the job's charges.") from None
            raise HTTPException(422, "A discount needs a description and an amount off above 0.") from None
        return
    db.add(LineItem(repair_order_id=ro.id, **line.model_dump()))
    db.flush()


class CustomerUpdate(BaseModel):
    """Only the fields sent change. Phone numbers stay as they are: they link the customer to texts and calls."""
    first_name: str | None = Field(default=None, min_length=1, max_length=64)
    last_name: str | None = Field(default=None, min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=128)
    address: str | None = Field(default=None, max_length=256)
    notes: str | None = Field(default=None, max_length=4000)
    customer_type: str | None = Field(default=None, pattern="^(individual|fleet)$")
    fleet_name: str | None = Field(default=None, max_length=128)

    _clean = field_validator("first_name", "last_name", "email", "address", "notes", "fleet_name",
                             mode="before")(_blank_to_none)


class JobIn(BaseModel):
    customer_id: int = Field(gt=0)
    vehicle_id: int = Field(gt=0)
    concern: str | None = Field(default=None, max_length=4000)
    mileage_in: int | None = Field(default=None, ge=0, le=5_000_000)


# Board columns the assistant may move a job to. Complete and Delivered (picked up) are left out
# on purpose: app.job_events can text the customer from them, so the office does those on the dashboard.
AGENT_STATUSES = {
    "intake": "Intake",
    "diagnosing": "Diagnosing",
    "waiting_parts": "Waiting Parts",
    "in_progress": "In Progress",
    "waiting_approval": "Waiting Approval",
    "approval_received": "Approval Received",
}
OFFICE_ONLY_STATUSES = ("complete", "delivered")


class JobStatusIn(BaseModel):
    status: str = Field(description="One of: " + ", ".join(AGENT_STATUSES) + ".")
    note: str | None = Field(default=None, max_length=200,
                             description="Why, or the next step, in a few words. Kept in the assistant's change log.")

    _clean = field_validator("note", mode="before")(_blank_to_none)


class DepositIn(BaseModel):
    amount: float = Field(gt=0, description="Dollars the customer already paid, e.g. 325.00.")
    method: str = Field(description="How they paid: one of the shop's payment methods, e.g. zelle, cash, quickbooks, "
                        "apple_cash, snap_finance, other.")
    paid_on: str | None = Field(default=None, description="Day the money came in, YYYY-MM-DD in shop time. Blank is today.")
    reference: str | None = Field(default=None, max_length=128,
                                  description="Optional confirmation number or note, e.g. a Zelle confirmation.")

    _clean = field_validator("paid_on", "reference", mode="before")(_blank_to_none)


class EstimateIn(BaseModel):
    customer_id: int = Field(gt=0)
    vehicle_id: int = Field(gt=0)
    concern: str | None = Field(default=None, max_length=4000)
    mileage_in: int | None = Field(default=None, ge=0, le=5_000_000)
    lines: list[LineIn] = Field(default_factory=list, max_length=60)


# ---------------------------------------------------------------- routes

@router.get("/whoami")
def whoami(key: AgentKey = Depends(agent("customers:read"))):
    return {"key": key.key_prefix + "…", "name": key.name, "scopes": key.scope_list,
            "expires_at": key.expires_at}


@router.get("/customers")
def search_customers(q: str = Query(default="", max_length=100), db: Session = Depends(get_db),
                     key: AgentKey = Depends(agent("customers:read"))):
    query = db.query(Customer)
    term = q.strip()
    digits = _digits(term)
    if len(digits) >= 7:
        found = [c for c in query.filter(Customer.phone.isnot(None)).all() if digits in _digits(c.phone)]
        return [customer_json(c) for c in found[:25]]
    if term:
        like = f"%{term}%"
        query = query.filter(or_(Customer.first_name.ilike(like), Customer.last_name.ilike(like),
                                 Customer.fleet_name.ilike(like), Customer.email.ilike(like)))
    return [customer_json(c) for c in query.order_by(Customer.id.desc()).limit(25).all()]


@router.get("/customers/{customer_id}")
def get_customer(customer_id: int, db: Session = Depends(get_db), key: AgentKey = Depends(agent("customers:read"))):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(404, "Customer not found.")
    return customer_json(customer, detail=True)


@router.post("/customers", status_code=201)
def create_customer(payload: CustomerIn, db: Session = Depends(get_db),
                    key: AgentKey = Depends(agent("customers:write"))):
    """A phone number already on file returns that customer instead of a duplicate."""
    digits = _digits(payload.phone)
    if len(digits) == 10:
        for existing in db.query(Customer).filter(Customer.phone.isnot(None)).all():
            if _digits(existing.phone) == digits:
                return {"existing": True, "customer": customer_json(existing, detail=True)}
    customer = Customer(first_name=payload.first_name.strip(), last_name=payload.last_name.strip(),
                        phone=payload.phone, email=payload.email, address=payload.address, notes=payload.notes,
                        customer_type=payload.customer_type, fleet_name=payload.fleet_name, name_source="staff")
    db.add(customer)
    db.flush()
    agent_keys.record(db, key, "customer.create", "customer", customer.id,
                      f"{customer.first_name} {customer.last_name}")
    db.commit()
    return {"existing": False, "customer": customer_json(customer, detail=True)}


@router.patch("/customers/{customer_id}")
def update_customer(customer_id: int, payload: CustomerUpdate, db: Session = Depends(get_db),
                    key: AgentKey = Depends(agent("customers:write"))):
    """Fill in or correct a customer's name and details, e.g. a record still named "Unknown (1234)"."""
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(404, "Customer not found.")
    changes = payload.model_dump(exclude_unset=True)
    for field in ("first_name", "last_name", "customer_type"):
        if field in changes and changes[field] is None:
            changes.pop(field)  # required columns: blank means leave as is
    changed = []
    for field, value in changes.items():
        if getattr(customer, field) != value:
            changed.append(f"{field}: {getattr(customer, field)!s:.40} -> {value!s:.40}")
            setattr(customer, field, value)
    if not changed:
        return {"changed": False, "customer": customer_json(customer, detail=True)}
    if "first_name" in changes or "last_name" in changes:
        customer.name_source = "staff"
    agent_keys.record(db, key, "customer.update", "customer", customer.id, "; ".join(changed))
    db.commit()
    return {"changed": True, "customer": customer_json(customer, detail=True)}


@router.post("/customers/{customer_id}/vehicles", status_code=201)
def add_vehicle(customer_id: int, payload: VehicleIn, db: Session = Depends(get_db),
                key: AgentKey = Depends(agent("customers:write"))):
    if db.get(Customer, customer_id) is None:
        raise HTTPException(404, "Customer not found.")
    if payload.vin:
        same = db.query(Vehicle).filter(Vehicle.customer_id == customer_id, Vehicle.vin == payload.vin).first()
        if same is not None:
            return {"existing": True, "vehicle": vehicle_json(same)}
    vehicle = Vehicle(customer_id=customer_id, **payload.model_dump())
    db.add(vehicle)
    db.flush()
    agent_keys.record(db, key, "vehicle.create", "vehicle", vehicle.id,
                      " ".join(str(p) for p in (vehicle.year, vehicle.make, vehicle.model) if p))
    db.commit()
    return {"existing": False, "vehicle": vehicle_json(vehicle)}


DECISIONS_DEFAULT_HOURS = 24
DECISIONS_MAX_DAYS = 14
CUSTOMER_SOURCES = (None, "link", "text")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def recent_decisions(db: Session, since: datetime | None = None, include_staff: bool = False,
                     now: datetime | None = None) -> dict:
    """Approve/decline decisions recorded since `since` (default the last 24 hours, at most 14 days back).

    Read only. By default only what customers decided themselves (approval link or text yes);
    include_staff adds what shop staff recorded in person or by phone. Newest job first.
    """
    now = _utc(now or datetime.now(timezone.utc))
    oldest = now - timedelta(days=DECISIONS_MAX_DAYS)
    since = _utc(since) if since else now - timedelta(hours=DECISIONS_DEFAULT_HOURS)
    since = max(since, oldest)
    # SQLite keeps times without a zone, so compare in UTC and re-check in Python.
    rows = (db.query(LineItem)
            .filter(LineItem.approval.in_(("approved", "declined")), LineItem.decided_at.isnot(None),
                    LineItem.decided_at >= since.replace(tzinfo=None) - timedelta(days=1))
            .all())
    jobs: dict[int, list[LineItem]] = {}
    for li in rows:
        if _utc(li.decided_at) < since:
            continue
        if not include_staff and li.approval_source not in CUSTOMER_SOURCES:
            continue
        jobs.setdefault(li.repair_order_id, []).append(li)
    out = []
    for ro_id, lines in jobs.items():
        ro = db.get(RepairOrder, ro_id)
        if ro is None:
            continue
        c, v = ro.customer, ro.vehicle
        approved = [li for li in lines if li.approval == "approved"]
        declined = [li for li in lines if li.approval == "declined"]

        def brief(li):
            return {"line_id": li.id, "description": li.description, "total": round(li.total, 2),
                    "source": li.approval_source or "link", "decided_at": _utc(li.decided_at).isoformat()}

        out.append({
            "job_id": ro.id, "job_status": ro.status,
            "customer": {"id": c.id, "name": " ".join(p for p in (c.first_name, c.last_name) if p), "phone": c.phone}
            if c else None,
            "vehicle": " ".join(str(p) for p in (v.year, v.make, v.model) if p) if v else None,
            "decided_at": max(_utc(li.decided_at) for li in lines).isoformat(),
            "approved": [brief(li) for li in approved], "declined": [brief(li) for li in declined],
            "approved_total": round(sum(li.total for li in approved), 2),
            "still_waiting": sum(1 for li in ro.line_items if li.approval == "pending"),
        })
    out.sort(key=lambda d: d["decided_at"], reverse=True)
    return {"since": since.isoformat(), "until": now.isoformat(), "count": len(out), "decisions": out}


@router.get("/decisions")
def list_decisions(since: datetime | None = Query(default=None), include_staff: bool = False,
                   db: Session = Depends(get_db), key: AgentKey = Depends(agent("estimates:read"))):
    return recent_decisions(db, since, include_staff)


@router.get("/jobs/{ro_id}")
def get_job(ro_id: int, db: Session = Depends(get_db), key: AgentKey = Depends(agent("estimates:read"))):
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    return job_json(ro)


def _new_job(db, customer_id, vehicle_id, concern, mileage_in):
    vehicle = db.get(Vehicle, vehicle_id)
    if db.get(Customer, customer_id) is None or vehicle is None:
        raise HTTPException(404, "Customer or vehicle not found.")
    if vehicle.customer_id != customer_id:
        raise HTTPException(422, "That vehicle belongs to another customer.")
    ro = RepairOrder(customer_id=customer_id, vehicle_id=vehicle_id, concern=concern,
                     mileage_in=mileage_in, public_token=secrets.token_urlsafe(32))
    db.add(ro)
    db.flush()
    return ro


@router.post("/jobs", status_code=201)
def open_job(payload: JobIn, db: Session = Depends(get_db), key: AgentKey = Depends(agent("estimates:write"))):
    """Open a job (repair order) with no lines, e.g. so an appointment can be booked for it."""
    ro = _new_job(db, payload.customer_id, payload.vehicle_id, payload.concern, payload.mileage_in)
    agent_keys.record(db, key, "job.open", "repair_order", ro.id, (payload.concern or "")[:200])
    db.commit()
    db.refresh(ro)
    return job_json(ro)


@router.post("/estimates", status_code=201)
def create_estimate(payload: EstimateIn, db: Session = Depends(get_db),
                    key: AgentKey = Depends(agent("estimates:write"))):
    """Start a job with draft estimate lines. Nothing is sent; staff send it for approval."""
    ro = _new_job(db, payload.customer_id, payload.vehicle_id, payload.concern, payload.mileage_in)
    for line in sorted(payload.lines, key=lambda x: x.item_type == "discount"):
        _add_line(db, ro, line, f"assistant:{key.name}"[:64])
        db.refresh(ro)
    agent_keys.record(db, key, "estimate.create", "repair_order", ro.id,
                      f"{len(payload.lines)} line(s); {(payload.concern or '')[:200]}")
    db.commit()
    db.refresh(ro)
    return job_json(ro)


@router.post("/jobs/{ro_id}/lines", status_code=201)
def add_lines(ro_id: int, lines: list[LineIn], db: Session = Depends(get_db),
              key: AgentKey = Depends(agent("estimates:write"))):
    if not lines or len(lines) > 60:
        raise HTTPException(422, "Send between 1 and 60 lines.")
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    if ro.invoices:
        raise HTTPException(409, "This job already has an invoice. Staff must review charges.")
    if ro.status in ("complete", "delivered"):
        raise HTTPException(409, "This job is finished. Staff must review charges.")
    actor = f"assistant:{key.name}"[:64]
    # Charges first, so a discount sent with them is measured against them.
    for line in sorted(lines, key=lambda x: x.item_type == "discount"):
        _add_line(db, ro, line, actor)
        db.refresh(ro)
    agent_keys.record(db, key, "estimate.add_lines", "repair_order", ro.id,
                      "; ".join(line.description[:60] for line in lines))
    db.commit()
    db.refresh(ro)
    return job_json(ro)


def _next_step(db: Session, ro: RepairOrder) -> dict | None:
    step = next_action.for_job(db, ro, "owner")
    return {"who": step["who"], "title": step["title"], "detail": step.get("detail")} if step else None


@router.post("/jobs/{ro_id}/status")
def set_job_status(ro_id: int, payload: JobStatusIn, db: Session = Depends(get_db),
                   key: AgentKey = Depends(agent("estimates:write"))):
    """Move a job to another board column, with the same checks as the job page's status menu.
    Nothing is sent to the customer."""
    target = (payload.status or "").strip().lower().replace(" ", "_").replace("-", "_")
    if target in OFFICE_ONLY_STATUSES:
        raise HTTPException(409, "Complete and Picked up can text the customer, so the office marks those on the dashboard.")
    if target not in AGENT_STATUSES:
        raise HTTPException(422, "Unknown status. Use one of: " + ", ".join(AGENT_STATUSES) + ".")
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    if ro.status in OFFICE_ONLY_STATUSES:
        raise HTTPException(409, f"This job is already {ro.status}. The office moves finished jobs on the dashboard.")
    blocked = ro_status_policy.status_error(ro, target, "owner")
    if blocked:
        raise HTTPException(*blocked)
    before = ro.status
    if target != before:
        ro.status = target
        if target != "intake":
            website_alerts.close_for_job(db, ro, f"assistant:{key.key_prefix}", f"job moved to {target}")
        summary = f"{before} -> {target}" + (f"; {payload.note}" if payload.note else "")
        agent_keys.record(db, key, "job.status", "repair_order", ro.id, summary)
        db.commit()
        db.refresh(ro)
    data = job_json(ro)
    data.update(previous_status=before, changed=target != before, next_step=_next_step(db, ro))
    return data


@router.post("/jobs/{ro_id}/deposits")
def record_deposit(ro_id: int, payload: DepositIn, db: Session = Depends(get_db),
                   key: AgentKey = Depends(agent("estimates:write"))):
    """Record a deposit the customer already paid on a job. It moves onto the invoice when the invoice is made.
    Nothing is sent to anyone."""
    from app import job_deposits
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    dep, error = job_deposits.record(
        db, ro, payload.amount, payload.method, payload.reference or "", payload.paid_on or "",
        by=f"assistant:{key.key_prefix}",
        before_commit=lambda d: agent_keys.record(db, key, "job.deposit", "repair_order", ro.id,
                                                  f"${d.amount:,.2f} {d.method}"))
    if error:
        raise HTTPException(409, error)
    db.refresh(ro)
    summary = job_deposits.summary(ro)
    return {"deposit": job_deposits.as_json(dep), "job": job_json(ro),
            "deposits_paid": summary["paid"], "deposit_due": summary["deposit_due"], "short": summary["short"]}


@router.get("/appointments")
def list_appointments(db: Session = Depends(get_db), key: AgentKey = Depends(agent("appointments:read"))):
    return [{"id": a.id, "repair_order_id": a.repair_order_id, "resource_id": a.resource_id,
             "starts_at": a.starts_at, "ends_at": a.ends_at, "status": a.status, "location": a.location}
            for a in db.query(Appointment).order_by(Appointment.starts_at).limit(250).all()]


@router.post("/appointments", status_code=201)
def book_appointment(payload: AppointmentInput, db: Session = Depends(get_db),
                     key: AgentKey = Depends(agent("appointments:write"))):
    """Same rules as the calendar: shop hours, buffers and no double-booking. Always tentative."""
    return create_appointment(db, payload, before_commit=lambda item: agent_keys.record(
        db, key, "appointment.create", "appointment", item.id,
        f"job {item.repair_order_id} at {item.starts_at.isoformat()} ({item.resource_id})"))


@router.post("/appointments/{appointment_id}/confirm")
def confirm_appointment(appointment_id: int, db: Session = Depends(get_db),
                        key: AgentKey = Depends(agent("appointments:confirm"))):
    """Only when the owner has said yes. Same checks as the calendar's Confirm button,
    and it adds the owner's reminder task. Nothing is sent to the customer."""
    return confirm(db, appointment_id, f"agent:{key.id}", f"assistant:{key.key_prefix}",
                   before_commit=lambda item: agent_keys.record(
                       db, key, "appointment.confirm", "appointment", item.id,
                       f"job {item.repair_order_id} at {item.starts_at.isoformat()}"))


class AppointmentChangeIn(BaseModel):
    action: str = Field(pattern="^(cancel|complete)$", description="cancel: the visit is off (any not-finished "
                        "appointment); complete: the visit happened (confirmed ones only).")


@router.post("/appointments/{appointment_id}/change")
def change_appointment(appointment_id: int, payload: AppointmentChangeIn, db: Session = Depends(get_db),
                       key: AgentKey = Depends(agent("appointments:write"))):
    """Cancel or complete a visit, as the calendar's buttons do: closes the owner's reminder task.
    Nothing is sent to the customer."""
    outcome = "cancelled" if payload.action == "cancel" else "completed"
    return close(db, appointment_id, outcome, f"agent:{key.id}", before_commit=lambda item: agent_keys.record(
        db, key, f"appointment.{payload.action}", "appointment", item.id,
        f"job {item.repair_order_id} at {item.starts_at.isoformat()}"))


class JobNotesIn(BaseModel):
    add_tech_note: str | None = Field(default=None, max_length=2000, description="Added as a new dated line at the "
                                      "end of the job's tech notes; what's there already is kept.")
    concern: str | None = Field(default=None, max_length=2000, description="Replaces the job's concern "
                                "(the customer's complaint), as on the job page.")

    _clean = field_validator("add_tech_note", "concern", mode="before")(_blank_to_none)


@router.post("/jobs/{ro_id}/notes")
def update_job_notes(ro_id: int, payload: JobNotesIn, db: Session = Depends(get_db),
                     key: AgentKey = Depends(agent("estimates:write"))):
    """Add a tech note and/or set the concern on a job. Nothing is sent to the customer."""
    if not (payload.add_tech_note or payload.concern):
        raise HTTPException(422, "Send add_tech_note or concern.")
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    changed = []
    if payload.add_tech_note:
        from app.shop_board import now_local

        stamp = now_local().strftime("%b %-d %-I:%M %p")
        line = f"{stamp} (Claude): {payload.add_tech_note}"
        ro.tech_notes = f"{ro.tech_notes.rstrip()}\n{line}" if (ro.tech_notes or "").strip() else line
        changed.append("tech note added")
    if payload.concern and payload.concern != ro.concern:
        ro.concern = payload.concern
        changed.append("concern set")
    agent_keys.record(db, key, "job.notes", "repair_order", ro.id, "; ".join(changed) or "no change")
    db.commit()
    db.refresh(ro)
    return {"id": ro.id, "concern": ro.concern, "tech_notes": ro.tech_notes, "changed": changed}


class SendApprovalIn(BaseModel):
    offer_snap: bool = False
    message: str | None = Field(default=None, max_length=1200)


SEND_NOTES = {
    "sending": "Handed to the shop line. It shows on the job once Quo sends it; the owner gets the usual Sent notice.",
    "already": "Not sent: their link already went out from the dashboard and they haven't texted since.",
    "texted": "Not sent: a text after their last message already has this job's link.",
    "paused": "Not sent: texting is paused on the dashboard.",
    "no_phone": "Not sent: the customer has no mobile number on file.",
    "nothing": "Not sent: no estimate lines are waiting on the customer.",
    "unavailable": "Not sent: couldn't read their text thread in Quo. Try again in a minute.",
}


@router.post("/jobs/{ro_id}/send-approval")
def send_approval(ro_id: int, payload: SendApprovalIn, db: Session = Depends(get_db),
                  key: AgentKey = Depends(agent("estimates:send"))):
    """Text the customer their approval link now, as the owner's Ask for approval button does."""
    from app import approval_links

    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    if ro.status == "delivered":
        raise HTTPException(409, "This job is already picked up.")
    actor = f"Claude connector {key.key_prefix}"[:64]
    message = (payload.message or "").strip() or None
    state, item = approval_links.send_now(db, ro, actor, key.created_by_user_id, offer_snap=payload.offer_snap,
                                          text=message, audit_detail=f"Claude connector, job {ro.id}")
    if state == "sending":
        agent_keys.record(db, key, "approval.send", "repair_order", ro.id, f"approval text #{item.id}")
        db.commit()
    db.refresh(ro)
    return {"state": state, "sent": state == "sending", "note": SEND_NOTES[state],
            "queue_item": item.id if item is not None else None,
            "text": item.draft_reply if state == "sending" else None, "job": job_json(ro)}
