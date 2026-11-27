"""Instant price on the public website (/site/quote).

Step 1 shows the price from the owner's price book in seconds and writes nothing.
Step 2 ("Hold this price") makes the same customer, vehicle, job, lead and owner item as an
estimate request, with the price the customer saw on the job as draft lines. The price is
always worked out again on the server; nothing the browser sends back is trusted as a price.
Nothing here texts, emails or calls anyone. Hidden unless INSTANT_QUOTES_ENABLED is "true".
"""
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import instant_quote, lead_lifecycle, website_alerts
from app.database import get_db
from app.public_site import FormError, SubmissionLimiter, clean_text, forms_enabled, mobile_zone
from app.routes.public_site import (CONTACT_OPTIONS, client_key, concern_text, find_or_create_customer,
                                    find_or_create_vehicle, fingerprint, form_values, new_repair_order, person_fields,
                                    recent_submission, remember_submission, render, vehicle_fields)

log = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/site")
PRICE_LIMIT = SubmissionLimiter(limit=40)  # looking at prices writes nothing, so it gets more room
HOLD_LIMIT = SubmissionLimiter(control="form_hourly_limit")
WHERE = {"shop": "At the shop", "onsite": "At my location"}


def _gate():
    if not instant_quote.enabled():
        raise HTTPException(404)


def _page(request: Request, status_code: int = 200, error: str = "", values: dict | None = None, quote=None):
    return render(request, "site/quote.html", status_code=status_code, form_page=True, active="estimate",
                  error=error, values=values or {}, jobs=instant_quote.jobs(), where_options=WHERE,
                  contact_options=CONTACT_OPTIONS, quote=quote)


def _work_out(db: Session, form) -> tuple[dict, dict, object]:
    """The job, the vehicle and the price, from the form. Raises FormError with a plain reason."""
    job = instant_quote.find(form.get("job") or "")
    if job is None:
        raise FormError("Choose a job.")
    vehicle = vehicle_fields(form, require_make=True)
    where = form.get("where") or "shop"
    if where not in WHERE:
        raise FormError("Choose where the work is done.")
    zone = mobile_zone(clean_text(form.get("zip"), 10)) if where == "onsite" else None
    if where == "onsite" and zone is None:
        raise FormError("We can't price on-site work for that ZIP code online. Send an estimate request instead.")
    quote = instant_quote.price(db, job, vehicle["year"], vehicle["make"],
                                vehicle["model"], where=where, diesel=form.get("diesel") == "on", zone=zone)
    if quote is None:
        raise FormError("We can't price this one instantly. Send an estimate request and we'll get back to you.")
    return job, vehicle, quote


@router.get("/quote", response_class=HTMLResponse)
def quote_form(request: Request):
    _gate()
    values = {}
    if request.query_params.get("job") and instant_quote.find(request.query_params["job"]):
        values["job"] = request.query_params["job"]
    return _page(request, values=values)


@router.post("/quote", response_class=HTMLResponse)
async def quote_price(request: Request, db: Session = Depends(get_db)):
    _gate()
    form = await request.form()
    values = form_values(form)
    if not PRICE_LIMIT.allow(client_key(request), time.time()):
        return _page(request, 429, "Too many price checks from this connection. Please call or text us.", values)
    try:
        _, _, quote = _work_out(db, values)
    except FormError as exc:
        return _page(request, 422, str(exc), values)
    log.info("instant_quote_shown job=%s total=%.2f", quote.job["key"], quote.total)
    return _page(request, values=values, quote=quote)


@router.post("/quote/hold", response_class=HTMLResponse)
async def quote_hold(request: Request, db: Session = Depends(get_db)):
    _gate()
    form = await request.form()
    values = form_values(form)
    if not forms_enabled(request.app.state.read_only):
        return _page(request, 503, "Online requests are not open yet. Please call or text us.", values)
    if values.get("company"):  # the hidden trap field: a bot
        request.session["site_estimate"] = {"ref": ""}
        return RedirectResponse("/site/estimate/received", status_code=303)
    try:
        job, vehicle, quote = _work_out(db, values)
    except FormError as exc:
        return _page(request, 422, str(exc), values)
    try:
        person = person_fields(form)
        when = clean_text(form.get("when"), 200)
    except FormError as exc:
        return _page(request, 422, str(exc), values, quote=quote)
    resend = fingerprint("quote", person["phone"], job["key"], vehicle["make"], vehicle["model"])
    if recent_submission(request, resend):
        request.session["site_estimate"] = recent_submission(request, resend)["item"]
        return RedirectResponse("/site/estimate/received", status_code=303)
    if not HOLD_LIMIT.allow(client_key(request), time.time()):
        return _page(request, 429, "Too many requests from this connection. Please call or text us.", values, quote=quote)
    where = values.get("where") or "shop"
    vehicle_text = " ".join(str(p) for p in (vehicle["year"], vehicle["make"], vehicle["model"]) if p)
    try:
        customer, matched = find_or_create_customer(db, person)
        car = find_or_create_vehicle(db, customer, matched, vehicle)
        concern = concern_text("Instant price from the website.", person, matched, [
            f"Job: {job['name']}.",
            f"Where: {WHERE[where]}{' (ZIP ' + clean_text(form.get('zip'), 10) + ')' if where == 'onsite' else ''}.",
            "Diesel engine: yes." if form.get("diesel") == "on" else "",
            f"Mileage given: {vehicle['mileage']:,}" if vehicle["mileage"] else "",
            f"When works for them: {when}" if when else "",
            instant_quote.owner_note(quote),
        ])
        order = new_repair_order(db, customer, car, concern, vehicle["mileage"])
        instant_quote.add_to_job(db, order, quote)
        item = website_alerts.queue(db, order, "request",
                                    f"Instant price ${quote.total:,.2f}: {job['name']}, {vehicle_text}. Check and send for approval."[:280],
                                    vehicle_text=vehicle_text)
        lead_lifecycle.intake_from_website(db, order, item)
        db.commit()
    except Exception:
        db.rollback()
        raise
    log.info("instant_quote_held job=%s order=%s total=%.2f", job["key"], order.id, quote.total)
    request.session["site_estimate"] = {"ref": f"WEB-{order.id}", "token": order.public_token, "roadside": False,
                                        "offer": 0, "quoted": f"${quote.total:,.2f}"}
    remember_submission(request, resend, "quote", request.session["site_estimate"])
    return RedirectResponse("/site/estimate/received", status_code=303)
