"""Instant website prices: the customer picks a job and a vehicle and sees a price in seconds.

The price is the owner's own numbers only, never a guess:
- labor hours from the owner's price book (book hours he looked up in ALLDATA), with
  per-vehicle hours where he has entered them;
- his labor rates from Business settings (shop, mobile, diesel);
- parts from what the shop last charged for the same parts on the same kind of vehicle
  (past job lines, which carry the AutoZone cost with no markup), else the price book's
  parts price;
- shop supplies on hour-based jobs (10% of labor, capped at $100), none on fixed-price
  packages such as the oil change and front brakes;
- tax at the one shop rate, on parts only.

A job with no hours or no parts price is not priced online; the site asks for an estimate
request instead. The price the customer saw goes on the job as draft lines (not sent for
approval). The owner checks the VIN, the ALLDATA hours and the parts, then sends it for
approval as usual. Nothing here texts, emails or calls anyone.

The price book is saved in Business settings (key "instant_quotes") as JSON; with nothing
saved, DEFAULTS is used. The website pages are hidden unless the Railway variable
INSTANT_QUOTES_ENABLED is "true".
"""
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app import business_settings, pricing

SETTING = "instant_quotes"
RATE_KINDS = ("shop", "diesel")
SUPPLIES_PERCENT = 10.0
SUPPLIES_CAP = 100.0
LAST_PAID_DAYS = 365
YEAR_SPREAD = 2  # a past job counts for "last paid" when the model year is this close
MAX_JOBS = 40
MAX_VEHICLES = 60


def enabled() -> bool:
    return os.getenv("INSTANT_QUOTES_ENABLED", "").strip().lower() == "true"


def _package(key, name, labor, parts, note=""):
    """A fixed-price package: set labor dollars, no supplies, the same price on every vehicle."""
    return {"key": key, "name": name, "hours": None, "labor": labor, "parts": parts, "rate": "shop",
            "supplies": False, "mobile": False, "parts_words": [], "note": note, "vehicles": []}


# Only prices the owner has already approved (canned jobs and published prices, Nov 25 2026).
# Every hour-based job, such as an alternator or a starter, waits until he enters its ALLDATA hours.
DEFAULTS = [
    _package("oil-change", "Oil change special (full synthetic, 5 quarts)", 12.05, 67.94,
             note="Covers 5 quarts and the filter. Extra quarts are $10.99 each."),
    _package("brakes-front-sedan", "Front brakes, pads and rotors (sedan)", 225.0, 194.0),
    _package("brakes-front-suv", "Front brakes, pads and rotors (SUV)", 225.0, 154.0),
    _package("brakes-front-pickup", "Front brakes, pads and rotors (pickup)", 225.0, 214.0),
    _package("diagnostic", "Diagnostic (full, in-shop)", 150.0, 0.0,
             note="$75 of it comes off the labor if you go ahead with the repair."),
]


class Invalid(ValueError):
    pass


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").casefold()).strip("-")[:48] or "job"


def _norm(text) -> str:
    """'F-150', 'f150' and 'F 150' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _number(value, low=0.0, high=100000.0):
    if value is None or value == "":
        return None
    try:
        number = float(str(value).lstrip("$").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def _clean_vehicle(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    make, model = " ".join(str(raw.get("make") or "").split())[:40], " ".join(str(raw.get("model") or "").split())[:60]
    years = raw.get("years") or [None, None]
    try:
        low, high = (int(years[0]) if years[0] else None), (int(years[1]) if years[1] else None)
    except (TypeError, ValueError, IndexError):
        return None
    hours, parts = _number(raw.get("hours"), 0, 200), _number(raw.get("parts"))
    if not make or (hours is None and parts is None):
        return None
    if low and high and low > high:
        low, high = high, low
    return {"make": make, "model": model, "years": [low, high], "hours": hours, "parts": parts}


def _clean_job(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("name") or "").split())[:80]
    if not name:
        return None
    vehicles = [v for v in (_clean_vehicle(v) for v in (raw.get("vehicles") or [])[:MAX_VEHICLES]) if v]
    words = [w for w in (" ".join(str(w).split()).casefold()[:40] for w in (raw.get("parts_words") or [])[:8]) if w]
    return {"key": slug(raw.get("key") or name), "name": name, "hours": _number(raw.get("hours"), 0, 200),
            "labor": _number(raw.get("labor"), 0, 20000), "parts": _number(raw.get("parts")), "rate": raw.get("rate") if raw.get("rate") in RATE_KINDS else "shop",
            "supplies": bool(raw.get("supplies")), "mobile": bool(raw.get("mobile")), "parts_words": words,
            "note": " ".join(str(raw.get("note") or "").split())[:200], "vehicles": vehicles}


def clean(raw) -> list[dict]:
    jobs, seen = [], set()
    for item in (raw if isinstance(raw, list) else [])[:MAX_JOBS]:
        job = _clean_job(item)
        if job and job["key"] not in seen:
            seen.add(job["key"])
            jobs.append(job)
    return jobs


def jobs() -> list[dict]:
    """The owner's saved price book, else the defaults. Never raises."""
    text = business_settings.raw(SETTING)
    if text is None:
        return clean(DEFAULTS)
    try:
        return clean(json.loads(text))
    except ValueError:
        return clean(DEFAULTS)


def find(key: str) -> dict | None:
    return next((j for j in jobs() if j["key"] == key), None)


def vehicle_entry(job: dict, year, make, model) -> dict | None:
    """The owner's hours for this exact vehicle, if he entered them. The most specific match wins."""
    best, best_score = None, -1
    for entry in job["vehicles"]:
        if _norm(entry["make"]) != _norm(make):
            continue
        if entry["model"] and _norm(entry["model"]) != _norm(model):
            continue
        low, high = entry["years"]
        if (low or high) and not year:
            continue
        if (low and year < low) or (high and year > high):
            continue
        score = (2 if entry["model"] else 0) + (1 if (low or high) else 0)
        if score > best_score:
            best, best_score = entry, score
    return best


def last_paid(db, job: dict, year, make, model, now: datetime | None = None) -> tuple[float, int] | None:
    """What the shop last charged for this job's parts on the same make and model (year within 2),
    from one past job in the last year. Returns (amount, job number) or None."""
    if db is None or not job["parts_words"] or not make or not model:
        return None
    from app.models import LineItem, RepairOrder, Vehicle
    since = (now or datetime.utcnow()) - timedelta(days=LAST_PAID_DAYS)
    rows = (db.query(LineItem, RepairOrder, Vehicle)
            .join(RepairOrder, LineItem.repair_order_id == RepairOrder.id)
            .join(Vehicle, RepairOrder.vehicle_id == Vehicle.id)
            .filter(LineItem.item_type == "part", RepairOrder.created_at >= since)
            .order_by(RepairOrder.created_at.desc(), RepairOrder.id.desc()).limit(2000).all())
    by_job: dict[int, float] = {}
    order: list[int] = []
    for line, ro, vehicle in rows:
        if _norm(vehicle.make) != _norm(make) or _norm(vehicle.model) != _norm(model):
            continue
        if year and vehicle.year and abs(vehicle.year - year) > YEAR_SPREAD:
            continue
        if line.approval == "declined" or line.unit_price is None or line.unit_price <= 0:
            continue
        text = (line.description or "").casefold()
        if not any(word in text for word in job["parts_words"]):
            continue
        if ro.id not in by_job:
            order.append(ro.id)
            by_job[ro.id] = 0.0
        by_job[ro.id] += (line.quantity or 0) * line.unit_price
    for ro_id in order:
        if by_job[ro_id] > 0:
            return round(by_job[ro_id], 2), ro_id
    return None


@dataclass
class Quote:
    job: dict
    lines: list = field(default_factory=list)  # (description, item_type, quantity, unit_price, taxable)
    hours: float = 0.0
    rate: float = 0.0
    rate_kind: str = "shop"
    hours_source: str = ""
    parts_source: str = ""
    mobile_fee: float = 0.0

    @property
    def parts(self) -> float:
        return round(sum(q * p for _, t, q, p, _ in self.lines if t == "part"), 2)

    @property
    def subtotal(self) -> float:
        return round(sum(q * p for _, _, q, p, _ in self.lines), 2)

    @property
    def tax(self) -> float:
        from app.routes.invoices import SHOP_TAX_RATE  # late import: invoices imports the app's routers
        return round(self.parts * SHOP_TAX_RATE, 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.tax, 2)

    def summary(self) -> str:
        return f"{self.job['name']}: ${self.total:,.2f} with tax"


def price(db, job: dict, year, make, model, where: str = "shop", diesel: bool = False, zone=None) -> Quote | None:
    """The instant price, or None when the owner's numbers don't cover this job and vehicle.
    where is "shop" or "onsite"; an on-site price needs the job marked mobile and a priced ZIP zone."""
    if job is None:
        return None
    entry = vehicle_entry(job, year, make, model)
    hours = entry["hours"] if entry and entry["hours"] is not None else job["hours"]
    fixed = job["labor"] if hours is None else None
    if hours is None and fixed is None:
        return None
    paid = None
    if entry and entry["parts"] is not None:
        parts, parts_source = entry["parts"], "your price book for this vehicle"
    else:
        paid = last_paid(db, job, year, make, model)
        if paid:
            parts, parts_source = paid[0], f"what job #{paid[1]} was charged for the same parts"
        else:
            parts, parts_source = job["parts"], "your price book"
    if parts is None:
        return None
    onsite = where == "onsite"
    if onsite and (not job["mobile"] or zone is None or zone.fee is None):
        return None
    quote = Quote(job=job, parts_source=parts_source)
    if fixed is not None:
        # A fixed-price package keeps its published price on every vehicle.
        labor = round(fixed, 2)
        quote.rate_kind, quote.hours_source = "package", "the package price"
        itemized = _package_lines(job["key"], labor, parts)
        if itemized:
            # Same numbers as the owner's package (Settings > Packages), so bill its lines: quarts and filter, not one lump.
            quote.lines.extend(itemized)
            parts = 0
        else:
            quote.lines.append((f"{job['name']} labor", "labor", 1, labor, False))
    else:
        kind = "mobile" if onsite else ("diesel" if diesel or job["rate"] == "diesel" else "shop")
        rate = pricing.rate_for(kind)
        labor = round(hours * rate, 2)
        quote.hours, quote.rate, quote.rate_kind = hours, rate, kind
        quote.hours_source = "your hours for this vehicle" if entry and entry["hours"] is not None else "your book hours"
        quote.lines.append((f"{job['name']} labor ({hours:g} hr at ${rate:,.0f}/hr)", "labor", 1, labor, False))
    if parts > 0:
        quote.lines.append((f"{job['name']} parts", "part", 1, round(parts, 2), True))
    if job["supplies"] and labor > 0:
        quote.lines.append(("Shop supplies", "misc", 1, round(min(labor * SUPPLIES_PERCENT / 100, SUPPLIES_CAP), 2), False))
    if onsite:
        call = pricing.rates()["service_call"]
        quote.mobile_fee = round(call + zone.fee, 2)
        quote.lines.append(("Mobile service call", "misc", 1, round(call, 2), False))
        if zone.fee:
            quote.lines.append(("Travel", "misc", 1, round(zone.fee, 2), False))
    return quote


def _package_lines(key: str, labor: float, parts: float) -> list:
    """The package's own lines when its labor and parts add up to this price, else []."""
    from app import canned_jobs
    package = canned_jobs.find(key)
    if not package:
        return []
    def total(kind, want):
        return round(sum(li["quantity"] * li["unit_price"] for li in package["lines"] if (li["item_type"] == "part") == want), 2)
    if total("part", False) != round(labor, 2) or total("part", True) != round(parts, 2):
        return []
    return [(li["description"], li["item_type"], li["quantity"], li["unit_price"], li["item_type"] == "part")
            for li in package["lines"]]


def add_to_job(db, ro, quote: Quote) -> list:
    """Puts the price the customer saw on the job as draft lines (not sent for approval). Caller commits."""
    from app.models import LineItem
    added = []
    for description, item_type, quantity, unit_price, taxable in quote.lines:
        li = LineItem(repair_order_id=ro.id, description=description, item_type=item_type, quantity=quantity,
                      unit_price=unit_price, taxable=taxable, approval=None)
        db.add(li)
        added.append(li)
    db.flush()
    return added


def owner_note(quote: Quote) -> str:
    """What the owner reads on the job and in his queue."""
    labor = ("labor at the package price" if quote.rate_kind == "package" else
             f"labor from {quote.hours_source} ({quote.hours:g} hr at ${quote.rate:,.0f}/hr, {quote.rate_kind})")
    return (f"Instant website price shown: ${quote.total:,.2f} with tax. {labor[0].upper() + labor[1:]}; "
            f"parts from {quote.parts_source}. "
            "Check the VIN, ALLDATA hours and parts, then press Ask customer to approve. "
            "After the customer approves, enter it in ALLDATA Shop Manager.")


# ── Settings page text ───────────────────────────────────────────────────────
# Each job's vehicle rows are plain text, one per row:  Make | Model | Years | Hours | Parts
# Model, years, hours or parts may be blank; years are "2011-2016" or "2015".
def vehicles_text(job: dict) -> str:
    rows = []
    for v in job["vehicles"]:
        low, high = v["years"]
        years = "" if not (low or high) else (str(low) if low == high or not high else f"{low or ''}-{high}")
        rows.append(" | ".join([v["make"], v["model"], years, "" if v["hours"] is None else f"{v['hours']:g}",
                                "" if v["parts"] is None else f"{v['parts']:.2f}"]))
    return "\n".join(rows)


def parse_vehicles(name: str, text: str) -> list[dict]:
    rows = []
    for number, row in enumerate((text or "").splitlines(), start=1):
        if not row.strip():
            continue
        cells = [c.strip() for c in row.split("|")]
        if len(cells) != 5:
            raise Invalid(f"{name}, vehicle row {number}: write it as Make | Model | Years | Hours | Parts.")
        make, model, years, hours, parts = cells
        match = re.fullmatch(r"(\d{4})?\s*(?:-\s*(\d{4}))?", years)
        if not match:
            raise Invalid(f"{name}, vehicle row {number}: years look like 2011-2016 or 2015.")
        low, high = match.group(1), match.group(2)
        if low and not high and "-" not in years:
            high = low
        for label, value, top in (("hours", hours, 200), ("parts", parts, 100000)):
            if value and _number(value, 0, top) is None:
                raise Invalid(f"{name}, vehicle row {number}: {label} must be a number.")
        entry = _clean_vehicle({"make": make, "model": model, "years": [low, high], "hours": hours, "parts": parts})
        if entry is None:
            raise Invalid(f"{name}, vehicle row {number}: needs a make and an hours or parts number.")
        rows.append(entry)
    return rows


def parse_form(form) -> list[dict]:
    """Rows named name_N, hours_N, parts_N, ... in order. A blank row is skipped."""
    result = []
    indexes = sorted({int(k.split("_", 1)[1]) for k in form.keys()
                      if k.startswith("name_") and k.split("_", 1)[1].isdigit()})
    for i in indexes:
        name = " ".join((form.get(f"name_{i}") or "").split())[:80]
        if form.get(f"remove_{i}") == "on" or not name:
            continue
        hours, parts = form.get(f"hours_{i}") or "", form.get(f"parts_{i}") or ""
        labor = form.get(f"labor_{i}") or ""
        if hours and _number(hours, 0, 200) is None:
            raise Invalid(f"{name}: hours must be a number.")
        if labor and _number(labor, 0, 20000) is None:
            raise Invalid(f"{name}: package labor must be a number.")
        if parts and _number(parts) is None:
            raise Invalid(f"{name}: parts must be a number.")
        words = [w.strip() for w in (form.get(f"words_{i}") or "").split(",") if w.strip()]
        result.append({"key": form.get(f"key_{i}") or slug(name), "name": name, "hours": hours, "labor": labor,
                       "parts": parts, "rate": form.get(f"rate_{i}") or "shop", "supplies": form.get(f"supplies_{i}") == "on",
                       "mobile": form.get(f"mobile_{i}") == "on", "parts_words": words,
                       "note": form.get(f"note_{i}") or "",
                       "vehicles": parse_vehicles(name, form.get(f"vehicles_{i}") or "")})
    if len(result) > MAX_JOBS:
        raise Invalid(f"At most {MAX_JOBS} jobs.")
    return clean(result)
