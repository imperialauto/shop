"""The U-Haul page: requests from U-Haul's emails, the jobs they become, the bill-out sheet and payments.

Nothing on these pages acts at U-Haul. Accepting a roadside offer happens on U-Haul's own page (the Open
offer button); "I took it" here records that and opens the job. Submitting the bill-out happens in the
vendor portal; "I submitted it" here records that. Nothing texts, calls or emails anyone.
"""
import json
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import uhaul
from app.appointments import PHOENIX
from app.auth import get_current_user
from app.database import get_db
from app.templating import Jinja2Templates

router = APIRouter()
intake_router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

NOTES = {
    "taken": "Job opened. Tap Leaving now when you head out.", "passed": "Marked as passed.",
    "step": "Time saved.", "undone": "Last step undone.", "saved": "Bill-out details saved.",
    "ready": "Ready to submit. Type it into the portal's Vendor RPO form, then tap I submitted it.",
    "submitted": "Recorded as submitted.", "paid": "Payment recorded.", "added": "Request added.",
    "shop": "No trip on this one. Tap Work done when it's finished.",
}


def _user(request: Request, db: Session, roles=uhaul.OFFICE):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return None
    if user.role not in roles:
        raise HTTPException(403, "U-Haul work is for the office.")
    return user


def _get(db: Session, req_id: int) -> uhaul.UhaulRequest:
    if not uhaul.ready(db):
        raise HTTPException(503, "U-Haul isn't switched on yet.")
    req = db.get(uhaul.UhaulRequest, req_id)
    if req is None:
        raise HTTPException(404, "That U-Haul request isn't here.")
    return req


def _local(value: datetime | None) -> str:
    """For a datetime-local box, in shop time."""
    return uhaul._aware(value).astimezone(PHOENIX).strftime("%Y-%m-%dT%H:%M") if value else ""


def _from_local(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=PHOENIX).astimezone(timezone.utc)
    except ValueError:
        raise ValueError("A time didn't read. Use the time picker.")


def _number(value, kind=float):
    value = str(value or "").strip().replace(",", "").replace("$", "")
    if not value:
        return None
    try:
        out = kind(float(value)) if kind is int else kind(value)
    except ValueError:
        raise ValueError(f"“{value}” isn't a number.")
    if out < 0:
        raise ValueError("Numbers can't be negative.")
    return out


def _back(req_id: int, note: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/uhaul/{req_id}?note={note}{anchor}", status_code=303)


def _error(request, db, user, req, message, status=422):
    return detail_page(request, db, user, req, error=message, status=status)


# ---------------------------------------------------------------- pages

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def list_page(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db, uhaul.STAFF)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if not uhaul.ready(db):
        return templates.TemplateResponse("uhaul/list.html", {"request": request, "user": user, "ready": False})
    now = datetime.now(timezone.utc)
    return templates.TemplateResponse("uhaul/list.html", {
        "request": request, "user": user, "ready": True, "b": uhaul.board(db, now), "now": now,
        "shown": lambda r: uhaul.shown_status(r, now), "label": uhaul.STAGE_LABEL, "kinds": uhaul.KIND_LABEL,
        "next_step": uhaul.NEXT_STEP, "unmatched": uhaul.unmatched_payments(db), "office": user.role in uhaul.OFFICE,
        "note": uhaul_note(request), "error": request.query_params.get("error", "")[:200],
        "intake_on": bool(uhaul.intake_key()), "age": age,
    })


def uhaul_note(request: Request) -> str:
    return NOTES.get(request.query_params.get("note", ""), "")


def age(when: datetime | None, now: datetime | None = None) -> str:
    if not when:
        return ""
    minutes = int(((now or datetime.now(timezone.utc)) - uhaul._aware(when)).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


def detail_page(request, db, user, req, error="", status=200):
    now = datetime.now(timezone.utc)
    sheet = uhaul.sheet(db, req) if req.repair_order_id else None
    times = {k: _local(getattr(req, f)) for k, f in uhaul.TIME_FIELD.items()}
    return templates.TemplateResponse("uhaul/detail.html", {
        "request": request, "user": user, "r": req, "shown": uhaul.shown_status(req, now), "label": uhaul.STAGE_LABEL,
        "kinds": uhaul.KIND_LABEL, "next_step": uhaul.NEXT_STEP.get(req.status), "sheet": sheet, "times": times,
        "local": lambda d: uhaul._local(d, "%a %b %d, %I:%M %p") if d else "", "age": age,
        "office": user.role in uhaul.OFFICE, "note": uhaul_note(request), "error": error,
        "paid": uhaul.paid_total(req), "short": uhaul.short_paid(req), "owed": uhaul.owed(req),
        "today": datetime.now(PHOENIX).date().isoformat(),
    }, status_code=status)


@router.get("/{req_id}", response_class=HTMLResponse)
def detail(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db, uhaul.STAFF)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return detail_page(request, db, user, _get(db, req_id))


# ---------------------------------------------------------------- taking it, passing, moving it on

@router.post("/{req_id}/take")
def take(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    req = _get(db, req_id)
    try:
        uhaul.take(db, req, user.username)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _error(request, db, user, req, str(exc), 409)
    return _back(req_id, "taken")


@router.post("/{req_id}/pass")
def pass_on(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    req = _get(db, req_id)
    try:
        uhaul.pass_on(db, req, user.username)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _error(request, db, user, req, str(exc), 409)
    back = request.query_params.get("back")
    return RedirectResponse("/uhaul?note=passed", status_code=303) if back == "list" else _back(req_id, "passed")


@router.post("/{req_id}/step")
async def step(req_id: int, request: Request, db: Session = Depends(get_db)):
    """Leaving now, On site, Work done, Back at shop. Techs can tap these too."""
    user = _user(request, db, uhaul.STAFF)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    req = _get(db, req_id)
    form = await request.form()
    to = str(form.get("to", ""))
    try:
        if to == "shop":
            uhaul.skip_travel(req)
        elif to == "undo":
            uhaul.undo_step(req)
        else:
            uhaul.step(req, to)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _error(request, db, user, req, str(exc), 409)
    if request.query_params.get("back") == "home":
        return RedirectResponse("/dashboard#uhaul-home", status_code=303)
    if request.query_params.get("back") == "list":
        return RedirectResponse("/uhaul?note=step", status_code=303)
    return _back(req_id, {"undo": "undone", "shop": "shop"}.get(to, "step"))


@router.post("/{req_id}/billing")
async def billing(req_id: int, request: Request, db: Session = Depends(get_db)):
    """The bill-out details only the shop knows: RPO number, odometer, travel basis, times, the three Cs."""
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    req = _get(db, req_id)
    if req.status in ("submitted", "paid"):
        return _error(request, db, user, req, "This one is already submitted. Its details stay as they were sent.", 409)
    form = await request.form()
    try:
        rpo = str(form.get("rpo_number", "")).strip().upper()
        if rpo and not uhaul._RPO.fullmatch(rpo):
            raise ValueError("That RPO number doesn't look right. It reads like 478-00508DA.")
        req.rpo_number = rpo or req.rpo_number
        req.hotline_ref = str(form.get("hotline_ref", "")).strip()[:32] or None
        req.equipment_number = " ".join(str(form.get("equipment_number", "")).split())[:48] or None
        req.is_trailer = form.get("is_trailer") == "on"
        req.odometer = 0 if req.is_trailer else _number(form.get("odometer"), int)
        basis = str(form.get("travel_basis", "hours"))
        req.travel_basis = basis if basis in ("hours", "miles") else "hours"
        req.travel_miles = _number(form.get("travel_miles")) if req.travel_basis == "miles" else None
        reached = uhaul.STAGES.index(req.status) if req.status in uhaul.STAGES else -1
        for stage, f in uhaul.TIME_FIELD.items():
            if f not in form:
                continue
            value = _from_local(str(form.get(f, "")))
            if value is None and stage == "en_route" and req.status == "en_route":
                raise ValueError("The job is en route, so it needs its leave time. Use Undo to step back.")
            if value is None and stage != "en_route" and reached >= uhaul.STAGES.index(stage):
                raise ValueError("A time for a step the job has passed can't be blank. Use Undo to step back.")
            if value is None and stage != "en_route" and reached < uhaul.STAGES.index(stage):
                continue  # a step not reached yet keeps no time
            if value is not None and reached < uhaul.STAGES.index(stage):
                raise ValueError("Tap the step on the job to stamp its time first.")
            setattr(req, f, value)
        order = [uhaul._aware(getattr(req, f)) for f in ("en_route_at", "on_site_at", "done_at", "back_at") if getattr(req, f)]
        if order != sorted(order):
            raise ValueError("The times are out of order. Leaving, on site, done and back go in that order.")
        for f in ("condition", "cause", "correction", "vendor_remarks"):
            setattr(req, f, str(form.get(f, "")).strip()[:2000] or None)
        db.commit()
    except ValueError as exc:
        db.rollback()
        db.refresh(req)
        return _error(request, db, user, req, str(exc))
    return _back(req_id, "saved", "#sheet")


@router.post("/{req_id}/ready")
def ready(req_id: int, request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    req = _get(db, req_id)
    try:
        uhaul.mark_ready(db, req)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _error(request, db, user, req, str(exc), 409)
    return _back(req_id, "ready", "#sheet")


@router.post("/{req_id}/submitted")
async def submitted(req_id: int, request: Request, db: Session = Depends(get_db)):
    """Records that the owner submitted it in U-Haul's portal. Submits nothing."""
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    req = _get(db, req_id)
    form = await request.form()
    try:
        amount = _number(form.get("amount"))
        if not amount:
            raise ValueError("Enter the total the portal showed when you submitted.")
        uhaul.mark_submitted(db, req, user.username, str(form.get("invoice", "")), amount)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _error(request, db, user, req, str(exc), 409)
    return _back(req_id, "submitted")


@router.post("/payments")
async def payment(request: Request, db: Session = Depends(get_db)):
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if not uhaul.ready(db):
        raise HTTPException(503, "U-Haul isn't switched on yet.")
    form = await request.form()
    try:
        paid_on = str(form.get("paid_on", "")).strip()
        pay, req = uhaul.record_payment(db, str(form.get("rpo_number", "")), _number(form.get("amount")),
                                        date.fromisoformat(paid_on) if paid_on else None,
                                        str(form.get("reference", "")), user.username)
        db.commit()
    except ValueError as exc:
        db.rollback()
        message = str(exc) if "isoformat" not in str(exc) else "That date didn't read."
        return RedirectResponse(f"/uhaul?error={message[:150]}#payments", status_code=303)
    if req is not None and form.get("from") == "detail":
        return _back(req.id, "paid")
    return RedirectResponse("/uhaul?note=paid#payments", status_code=303)


@router.post("/new")
async def add_by_hand(request: Request, db: Session = Depends(get_db)):
    """A call from U-Haul's hotline or a PO read over the phone: add it without an email."""
    user = _user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if not uhaul.ready(db):
        raise HTTPException(503, "U-Haul isn't switched on yet.")
    form = await request.form()
    kind = str(form.get("kind", "roadside"))
    rpo = str(form.get("rpo_number", "")).strip().upper()
    name = " ".join(str(form.get("customer_name", "")).split())[:128]
    if kind not in uhaul.KIND_LABEL:
        kind = "roadside"
    if rpo and not uhaul._RPO.fullmatch(rpo):
        return RedirectResponse("/uhaul?error=That RPO number doesn't look right. It reads like 478-00508DA.#add", status_code=303)
    if kind == "rpo" and not rpo:
        return RedirectResponse("/uhaul?error=A repair PO needs its RPO number.#add", status_code=303)
    if kind == "roadside" and not (name or str(form.get("hotline_ref", "")).strip()):
        return RedirectResponse("/uhaul?error=Add the U-Haul customer's name or the hotline number.#add", status_code=303)
    now = datetime.now(timezone.utc)
    try:
        amount = _number(form.get("estimated_amount"))
    except ValueError as exc:
        return RedirectResponse(f"/uhaul?error={str(exc)[:150]}#add", status_code=303)
    req = uhaul.UhaulRequest(kind=kind, status="new", sender=f"added by {user.username}"[:200], received_at=now,
                             last_seen_at=now, customer_name=name or None, rpo_number=rpo or None,
                             hotline_ref=str(form.get("hotline_ref", "")).strip()[:32] or None,
                             equipment_number=" ".join(str(form.get("equipment_number", "")).split())[:48] or None,
                             estimated_amount=amount,
                             reason=str(form.get("reason", "")).strip()[:2000] or None,
                             location=str(form.get("location", "")).strip()[:600] or None)
    db.add(req)
    db.commit()
    return _back(req.id, "added")


# ---------------------------------------------------------------- intake from the business mailbox

@intake_router.post("/uhaul")
async def intake(request: Request, db: Session = Depends(get_db)):
    """The mailbox script posts each U-Haul email here as JSON. 503 means try again later; nothing was lost.

    Body: {"message_id", "thread_id", "from", "subject", "date" (ISO 8601), "body" (plain text), "links": [...]}
    Header: X-Uhaul-Intake-Key with the UHAUL_INTAKE_KEY value. Without that setting the address is off (404)."""
    if not uhaul.intake_key():
        raise HTTPException(404)
    if not uhaul.key_ok(request.headers.get("x-uhaul-intake-key")):
        return JSONResponse({"error": "bad key"}, status_code=401)
    raw = await request.body()
    if len(raw) > 200_000:
        return JSONResponse({"error": "too large"}, status_code=413)
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return JSONResponse({"error": "not JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "not an object"}, status_code=400)
    try:
        if not uhaul.ready(db):
            return JSONResponse({"error": "not switched on"}, status_code=503)
        outcome, req = uhaul.receive(db, payload)
        db.commit()
    except IntegrityError:  # the same email posted twice at once: the first one is kept
        db.rollback()
        return JSONResponse({"outcome": "duplicate"})
    except SQLAlchemyError:
        db.rollback()
        return JSONResponse({"error": "database"}, status_code=503)
    return JSONResponse({"outcome": outcome, "id": req.id if req else None})
