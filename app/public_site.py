"""Public website content and input rules for the replacement imperialautophx.com.

Pure data and validation only: no database access, no network calls and no
customer messages. Everything the public pages say about the business lives
here so the owner can review it in one place before cutover.
"""
import json
import os
import re
from dataclasses import dataclass, field, replace

from app import business_settings

PRODUCTION_PROJECT_ID = "97a71288-74a7-4405-9789-b74fda7b8939"

# Suite confirmed by the owner on 2026-11-05. Phone, email and hours match the owner's records.
BUSINESS = {
    "name": "Imperial Auto Care",
    "legal_name": "Imperial Auto Care LLC",
    "phone_display": "(480) 914-4144",
    "phone_tel": "+14809144144",
    "email": "jaelan@imperialautocarellc.com",
    "street": "7915 N Glen Harbor Blvd, Suite 508",
    "city": "Glendale",
    "region": "AZ",
    "postal": "85307",
    "hours": [
        ("Monday – Friday", "11 AM – 7 PM"),
        ("Saturday", "12 PM – 5 PM"),
        ("Sunday", "Closed, except qualifying emergency roadside service"),
    ],
}


@dataclass(frozen=True)
class ServiceLine:
    slug: str
    name: str
    short: str
    intro: str
    points: tuple[str, ...]
    note: str = ""
    division: str = "Imperial Auto Care"


SERVICE_LINES: tuple[ServiceLine, ...] = (
    ServiceLine(
        "auto-repair", "Auto repair and diagnostics",
        "Check engine lights, drivability, electrical and everyday repairs on gas cars, trucks and SUVs.",
        "Bring it to the shop and we find the cause before we replace parts. You get a written estimate before work starts.",
        ("Check engine light and drivability diagnostics", "Electrical and wiring faults",
         "Brakes, steering and suspension", "Cooling system, A/C and heating", "Maintenance and inspections"),
    ),
    ServiceLine(
        "diesel", "Diesel repair",
        "Powerstroke, Cummins and Duramax pickups, including the jobs other shops send away.",
        "We take on diesel work that other shops turn down, from fuel system faults to no-starts.",
        ("6.0L and 6.7L Powerstroke", "Cummins", "Duramax", "Fuel system and injection diagnostics",
         "Cooling, electrical and starting problems"),
    ),
    ServiceLine(
        "european-sprinter", "European and Sprinter",
        "Mercedes Sprinter vans and European vehicles, diagnosed with the right tools.",
        "Sprinter vans and European vehicles get the same diagnose-first approach, with an estimate before any work.",
        ("Mercedes-Benz Sprinter, gas and diesel", "BMW and Mercedes-Benz", "Module and network diagnostics",
         "Cooling, turbo and fuel system repairs"),
    ),
    ServiceLine(
        "dps6-transmission", "Ford DPS6 PowerShift",
        "Clutch and shudder repairs for the Focus and Fiesta dual-clutch transmission.",
        "DPS6 work is done in the shop and starts with a diagnostic so we quote the right repair.",
        ("Shudder, slipping and hesitation diagnosis", "Clutch replacement", "Shop-only repair"),
        note="A diagnostic comes first on every DPS6 job.",
    ),
    ServiceLine(
        "fleet", "Fleet service",
        "Maintenance and repairs for work trucks, vans and rental fleets.",
        "Keep vehicles earning. We work with fleet and rental owners on scheduled service, inspections and repairs.",
        ("Fleet maintenance and inspections", "Rental and car-share fleet vehicles", "Work trucks and vans",
         "One account for every vehicle"),
    ),
    ServiceLine(
        "mobile-roadside", "Mobile and roadside",
        "Selected on-site repairs and Class 8 roadside calls when a vehicle cannot come to the shop.",
        "Most work is done at the shop. When a vehicle cannot get here, ask us about an on-site visit or a bring-in.",
        ("Class 8 semi roadside: starters, alternators, belts, water pumps and air systems",
         "Selected on-site repairs", "Shop transport for vehicles coming in for major repairs"),
        note="Mobile and roadside work is quoted by request. Call or send an estimate request.",
    ),
    ServiceLine(
        "towing", "Towing and roadside dispatch",
        "Towing is available through Imperial Auto Care. Send the location and we will confirm your request.",
        "Broken down or need a vehicle moved? Send the location and vehicle details. If you are in danger, call 911 first.",
        ("Towing requests", "Roadside help requests", "Vehicle bring-ins for repairs"),
        note="Dispatch requests need staff confirmation. Nothing is dispatched until we confirm it with you.",
    ),
    ServiceLine(
        "rentals", "Vehicle rentals",
        "Rental vehicles for gig drivers. Ask about current availability.",
        "We rent vehicles to gig drivers. Send a request and we will go over current vehicles, rates and requirements.",
        ("Rentals for gig drivers", "Current availability by request"),
        note="Rental rates are shared when you ask, not published here yet.",
    ),
    ServiceLine(
        "nvzn-customs", "NVZN Customs",
        "Window tint, wraps, paint protection film and detailing.",
        "NVZN Customs is our appearance division for tint, wraps, paint protection film and detailing.",
        ("Window tint", "Vinyl wraps", "Paint protection film (PPF)", "Detailing and paint correction",
         "Ceramic coating"),
        division="NVZN Customs",
    ),
)

SERVICE_LINE_BY_SLUG = {line.slug: line for line in SERVICE_LINES}


@dataclass(frozen=True)
class BookableService:
    """A standard service a customer may book online without talking to us first."""
    slug: str
    name: str
    description: str
    minutes: int
    price: float | None = None
    price_type: str = "exact"  # "exact" for predictable work, "starting" for variable work
    mobile: bool = False
    # Vehicle-class starting prices for a package (e.g. brakes): {"sedan": 349, ...}. Never a final price.
    tiers: tuple = ()
    per: str = ""  # unit the price covers, e.g. "axle"

    @property
    def price_label(self) -> str:
        if self.price is None:
            return "Price coming soon"
        amount = f"${self.price:,.2f}".removesuffix(".00")
        label = f"From {amount}" if self.price_type == "starting" else amount
        return f"{label} per {self.per}" if self.per else label

    def tier(self, key: str):
        return next((t for t in self.tiers if t[0] == key), None)


# Owner approved these six on 2026-11-05, subject to final prices and times.
# Final values come from PUBLIC_SERVICE_CATALOG. No price is ever assumed here.
BOOKABLE_SERVICES: tuple[BookableService, ...] = (
    BookableService("diagnostic", "Diagnostic appointment", "Check engine light, warning lights, noises or drivability. We find the cause and give you an estimate.", 60),
    BookableService("oil-service", "Oil and filter service", "Gas or diesel. Includes a quick look over the vehicle.", 60),
    BookableService("brake-inspection", "Brake inspection", "Pads, rotors, fluid and hardware checked, with an estimate if anything needs work.", 60),
    BookableService("ac-check", "A/C performance check", "We check how the A/C is cooling and look for leaks.", 60),
    BookableService("battery-charging", "Battery and charging test", "Battery, starter and alternator tested.", 30),
    BookableService("pre-purchase", "Pre-purchase inspection", "A full look over a vehicle before you buy it.", 90),
    # Package with vehicle-class starting prices. Parts fitment, labor time and margin are checked
    # by the shop before the price is final, so it is always shown as a starting price.
    BookableService("brake-pads-rotors", "Brake pads and rotors", "New pads and rotors on one axle. The final price is confirmed after we check parts for your exact vehicle.", 120,
                    price_type="starting", per="axle"),
)

VEHICLE_CLASSES = {"sedan": "Sedan or compact", "suv": "SUV or crossover", "pickup": "Pickup or large SUV"}


def preview_mode() -> bool:
    """A disposable preview may test unpriced booking. It never works in production."""
    return (os.getenv("PUBLIC_PREVIEW_MODE", "").lower() == "true"
            and os.getenv("RAILWAY_ENVIRONMENT_NAME", "").lower() != "production"
            and os.getenv("RAILWAY_PROJECT_ID") != PRODUCTION_PROJECT_ID)


def preview_code() -> str:
    return os.getenv("PREVIEW_ACCESS_CODE", "") if preview_mode() else ""


def booking_closed_days() -> set[int]:
    """Weekdays closed to online booking, 0 is Monday. Shop hours already close Sunday."""
    raw = business_settings.raw("booking_closed_days") or ""
    return {int(day) for day in raw.split(",") if day.strip().isdigit()}


def service_catalog() -> dict[str, dict]:
    """PUBLIC_SERVICE_CATALOG: {"diagnostic": {"price": 0, "price_type": "exact", "minutes": 60, "mobile": false}}"""
    try:
        data = json.loads(business_settings.raw("booking_services") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _with_catalog(service: BookableService, entry: dict) -> BookableService:
    try:
        price = None if entry.get("price") is None else float(entry["price"])
        minutes = int(entry.get("minutes", service.minutes))
    except (TypeError, ValueError):
        return service
    if (price is not None and price < 0) or not 15 <= minutes <= 480:
        return service
    kind = "starting" if entry.get("price_type") == "starting" or service.tiers or entry.get("tiers") else "exact"
    tiers = ()
    if isinstance(entry.get("tiers"), dict):
        try:
            tiers = tuple((k, VEHICLE_CLASSES[k], float(v)) for k, v in entry["tiers"].items() if k in VEHICLE_CLASSES and float(v) >= 0)
        except (TypeError, ValueError):
            tiers = ()
        if tiers and price is None:
            price = min(t[2] for t in tiers)
        kind = "starting"
    return replace(service, price=price, minutes=minutes, price_type=kind, mobile=entry.get("mobile") is True, tiers=tiers)


def approved_bookable_services() -> list[BookableService]:
    """PUBLIC_BOOKING_SERVICES is a comma list of slugs. Unset means all six approved services."""
    catalog = service_catalog()
    raw = os.getenv("PUBLIC_BOOKING_SERVICES")
    wanted = None if raw is None else {part.strip() for part in raw.split(",") if part.strip()}
    return [_with_catalog(s, catalog.get(s.slug) or {}) for s in BOOKABLE_SERVICES if wanted is None or s.slug in wanted]


def _unpriced_preview() -> bool:
    """A disposable preview with no prices at all may book unpriced services to test the flow.
    Once any price is configured, the preview behaves exactly like production."""
    return preview_mode() and not any(isinstance(e, dict) and (e.get("price") is not None or e.get("tiers"))
                                      for e in service_catalog().values())


def bookable_services() -> list[BookableService]:
    """Only services with a price can be booked, except in an unpriced disposable preview."""
    return [s for s in approved_bookable_services() if s.price is not None or _unpriced_preview()]


def bookable_service(slug: str) -> BookableService | None:
    return next((s for s in bookable_services() if s.slug == slug), None)


def requestable_services() -> list[BookableService]:
    """Approved standard services without a final price yet. They are requested online, not booked,
    so the customer is never sent to the phone just because a price isn't set."""
    return [s for s in approved_bookable_services() if s.price is None and not _unpriced_preview()]


def auto_times() -> dict:
    """PUBLIC_AUTO_TIMES: {"resources": ["bay-1"], "step_minutes": 30}. Offers open times straight from
    shop hours, so nobody has to publish slots by hand. Empty means only published slots are offered."""
    try:
        data = json.loads(business_settings.raw("booking_auto_times") or "{}")
        resources = [str(r).strip()[:80] for r in data.get("resources", []) if str(r).strip()]
        step = int(data.get("step_minutes", 30))
    except (ValueError, TypeError, AttributeError):
        return {}
    if not resources or step not in (15, 20, 30, 60):
        return {}
    return {"resources": resources[:4], "step_minutes": step}


@dataclass(frozen=True)
class MobileZone:
    fee: float | None  # None only in a preview, where no fee has been verified
    travel_minutes: int


def mobile_zone(zip_code: str | None) -> MobileZone | None:
    """PUBLIC_MOBILE_ZONES: {"85000": {"fee": 0, "travel_minutes": 30}}. Unlisted ZIP codes are not served online."""
    try:
        zones = json.loads(business_settings.raw("mobile_zones") or "{}")
        entry = zones.get((zip_code or "").strip())
        if not isinstance(entry, dict):
            return None
        fee = None if entry.get("fee") is None and preview_mode() else float(entry["fee"])
        travel = int(entry["travel_minutes"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return MobileZone(fee, travel) if (fee is None or fee >= 0) and 0 <= travel <= 240 else None


def forms_enabled(read_only: bool) -> bool:
    """Public forms only write when the app is writable and the owner switched them on."""
    return not read_only and os.getenv("PUBLIC_FORMS_ENABLED", "").strip().lower() == "true"


def indexable() -> bool:
    """Search engines stay out until cutover so the live Squarespace site keeps its ranking."""
    return os.getenv("PUBLIC_SITE_INDEXABLE", "").strip().lower() == "true"


# ── Input rules ──────────────────────────────────────────────────────────────
VIN_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
NAME_LIMIT = 64
TEXT_LIMIT = 2000


class FormError(ValueError):
    pass


def clean_text(value: str | None, limit: int) -> str:
    text = " ".join((value or "").split()) if limit <= 128 else (value or "").strip()
    return text[:limit]


def normalize_phone(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        raise FormError("Enter a 10-digit US phone number.")
    return digits


def phone_digits(value: str | None) -> str:
    """Comparable digits for an already stored phone. Never raises."""
    digits = re.sub(r"\D", "", value or "")
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def format_phone(digits: str) -> str:
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def normalize_vin(value: str | None) -> str:
    vin = re.sub(r"[\s-]", "", value or "").upper()
    if not vin:
        return ""
    if not VIN_PATTERN.match(vin):
        raise FormError("A VIN is 17 letters and numbers and never uses I, O or Q. Leave it blank if you are not sure.")
    return vin


def normalize_email(value: str | None) -> str:
    email = (value or "").strip()
    if not email:
        return ""
    if len(email) > 128 or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise FormError("Check the email address.")
    return email


def normalize_year(value: str | None, this_year: int) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    if not text.isdigit() or not 1950 <= int(text) <= this_year + 1:
        raise FormError("Check the vehicle year.")
    return int(text)


def normalize_mileage(value: str | None) -> int | None:
    text = re.sub(r"[,\s]", "", value or "")
    if not text:
        return None
    if not text.isdigit() or int(text) > 2_000_000:
        raise FormError("Check the mileage.")
    return int(text)


@dataclass
class SubmissionLimiter:
    """Caps public form submissions per address. In memory, like the login limiter."""
    limit: int = field(default_factory=lambda: int(os.getenv("PUBLIC_FORM_HOURLY_LIMIT") or 5))
    window_seconds: int = 3600
    seen: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, key: str, now: float) -> bool:
        recent = [t for t in self.seen.get(key, []) if now - t < self.window_seconds]
        if len(recent) >= self.limit:
            self.seen[key] = recent
            return False
        recent.append(now)
        self.seen[key] = recent
        return True
