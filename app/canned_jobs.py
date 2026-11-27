"""Canned jobs: one tap on the job page adds a whole package (an oil change, a front brake job, a
diagnostic) with its parts and labor lines already filled in.

The packages are the owner's to change on /settings/canned-jobs. They are saved in Business settings
(key "canned_jobs") as JSON; with nothing saved, the defaults below are used. Lines go on the job as
ordinary draft lines (not sent for approval), so they can be edited or removed like any other line
before the estimate goes out. Tax follows the shop rule everywhere else: parts only, at the one shop
rate. Nothing here texts or sends anything.

Default prices, owner-approved Nov 25 2026:
- Oil change special $79.99 plus tax: quarts used at the per-quart price ($10.99 full synthetic) plus
  the filter as its own line ($12.99), plus a labor line that brings 5 quarts to $79.99.
- Front brake job, published prices sedan $419, SUV $379, pickup $439: 1.5 hours labor at the $150
  shop rate ($225, the brake labor minimum per axle on the price sheet) plus the pads and rotors.
- Diagnostic $150 (the full diagnostic).
"""
import json
import re

from app import business_settings

SETTING = "canned_jobs"
ITEM_TYPES = ("labor", "part", "sublet", "misc")
MAX_PACKAGES = 30
MAX_LINES = 12


def _line(description, item_type, quantity, unit_price):
    return {"description": description, "item_type": item_type, "quantity": quantity,
            "unit_price": unit_price, "taxable": item_type == "part"}


def _brake(key, label, price):
    labor_hours, rate = 1.5, 150.0
    return {"key": key, "name": f"Front brake job, {label}",
            "lines": [_line("Front brake pads and rotors labor", "labor", labor_hours, rate),
                      _line("Front brake pads and rotors (parts)", "part", 1, round(price - labor_hours * rate, 2))]}


DEFAULTS = [
    {"key": "oil-change", "name": "Oil change special",
     "lines": [_line("Full synthetic oil (per quart)", "part", 5, 10.99),
               _line("Oil filter", "part", 1, 12.99),
               _line("Oil change labor", "labor", 1, 12.05)]},
    _brake("brakes-front-sedan", "sedan", 419.0),
    _brake("brakes-front-suv", "SUV", 379.0),
    _brake("brakes-front-pickup", "pickup", 439.0),
    {"key": "diagnostic", "name": "Diagnostic",
     "lines": [_line("Diagnostic (full, in-shop)", "labor", 1, 150.0)]},
]


class Invalid(ValueError):
    pass


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").casefold()).strip("-")[:48] or "package"


def _clean_line(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    try:
        description = " ".join(str(raw.get("description") or "").split())[:200]
        item_type = str(raw.get("item_type") or "labor")
        quantity = round(float(raw.get("quantity", 1)), 2)
        unit_price = round(float(raw.get("unit_price", 0)), 2)
    except (TypeError, ValueError):
        return None
    if not description or item_type not in ITEM_TYPES or not 0 < quantity <= 1000:
        return None
    if not 0 <= unit_price <= 100000 and not (item_type == "misc" and -100000 <= unit_price < 0):
        return None
    return _line(description, item_type, quantity, unit_price)


def clean(data) -> list[dict]:
    """Keeps only well-formed packages and lines, with unique keys."""
    packages, keys = [], set()
    for raw in data if isinstance(data, list) else []:
        if not isinstance(raw, dict):
            continue
        name = " ".join(str(raw.get("name") or "").split())[:80]
        lines = [line for line in (_clean_line(li) for li in (raw.get("lines") or [])[:MAX_LINES]) if line]
        if not name or not lines:
            continue
        key = slug(str(raw.get("key") or name))
        while key in keys:
            key += "-2"
        keys.add(key)
        packages.append({"key": key, "name": name, "lines": lines})
        if len(packages) >= MAX_PACKAGES:
            break
    return packages


def packages() -> list[dict]:
    """The owner's saved packages, else the defaults. Never raises."""
    text = business_settings.raw(SETTING)
    if text is None:
        return clean(DEFAULTS)
    try:
        return clean(json.loads(text))
    except ValueError:
        return clean(DEFAULTS)


def find(key: str) -> dict | None:
    return next((p for p in packages() if p["key"] == key), None)


def totals(package: dict) -> dict:
    """Subtotal, tax (parts only, the one shop rate) and total, the way the invoice works them out."""
    from app.routes.invoices import SHOP_TAX_RATE  # late import: invoices imports the app's routers
    subtotal = round(sum(li["quantity"] * li["unit_price"] for li in package["lines"]), 2)
    parts = sum(li["quantity"] * li["unit_price"] for li in package["lines"] if li["item_type"] == "part")
    tax = round(parts * SHOP_TAX_RATE, 2)
    return {"subtotal": subtotal, "tax": tax, "total": round(subtotal + tax, 2)}


def menu() -> list[dict]:
    """What the job page's Add a package list shows. Never raises."""
    try:
        return [{"key": p["key"], "name": p["name"], **totals(p)} for p in packages()]
    except Exception:  # a bad saved value must never break the job page
        return []


def add_to_job(db, ro, key: str) -> list:
    """Adds the package's lines to the job as draft lines (not sent for approval). Caller commits."""
    from app.models import LineItem
    package = find(key)
    if package is None:
        raise Invalid("unknown")
    added = []
    for line in package["lines"]:
        li = LineItem(repair_order_id=ro.id, description=line["description"], item_type=line["item_type"],
                      quantity=line["quantity"], unit_price=line["unit_price"], taxable=line["taxable"],
                      approval=None)
        db.add(li)
        added.append(li)
    db.flush()
    for li in added:
        if li not in ro.line_items:
            ro.line_items.append(li)
    return added


# The settings page edits each package's lines as plain text, one line per row:
#   Description | type | quantity | unit price
def lines_text(package: dict) -> str:
    return "\n".join(f"{li['description']} | {li['item_type']} | {li['quantity']:g} | {li['unit_price']:.2f}"
                     for li in package["lines"])


def parse_lines(name: str, text: str) -> list[dict]:
    rows = []
    for number, row in enumerate((text or "").splitlines(), start=1):
        if not row.strip():
            continue
        cells = [c.strip() for c in row.split("|")]
        if len(cells) != 4:
            raise Invalid(f"{name}, row {number}: write it as Description | type | quantity | price.")
        description, item_type, quantity, price = cells
        item_type = item_type.casefold()
        if item_type not in ITEM_TYPES:
            raise Invalid(f"{name}, row {number}: type must be labor, part, sublet or misc.")
        try:
            raw = {"description": description, "item_type": item_type, "quantity": float(quantity),
                   "unit_price": float(price.lstrip("$").replace(",", ""))}
        except ValueError:
            raise Invalid(f"{name}, row {number}: quantity and price must be numbers.") from None
        line = _clean_line(raw)
        if line is None:
            raise Invalid(f"{name}, row {number}: needs a description, a quantity above 0 and a price of 0 or more.")
        rows.append(line)
    if len(rows) > MAX_LINES:
        raise Invalid(f"{name}: at most {MAX_LINES} lines per package.")
    return rows


def parse_form(form) -> list[dict]:
    """Rows named name_N / lines_N / remove_N, in order. A blank row is skipped."""
    result = []
    indexes = sorted({int(k.split("_", 1)[1]) for k in form.keys()
                      if k.startswith("name_") and k.split("_", 1)[1].isdigit()})
    for i in indexes:
        name = " ".join((form.get(f"name_{i}") or "").split())[:80]
        text = form.get(f"lines_{i}") or ""
        if form.get(f"remove_{i}") == "on" or (not name and not text.strip()):
            continue
        if not name:
            raise Invalid(f"Package {i + 1}: give it a name.")
        lines = parse_lines(name, text)
        if not lines:
            raise Invalid(f"{name}: add at least one line.")
        result.append({"key": form.get(f"key_{i}") or slug(name), "name": name, "lines": lines})
    if len(result) > MAX_PACKAGES:
        raise Invalid(f"At most {MAX_PACKAGES} packages.")
    return clean(result)
