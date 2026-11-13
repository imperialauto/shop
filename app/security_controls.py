"""Security & Privacy Center: every protection the owner can see, change or turn off.

One saved JSON setting (`security_controls`) holds only what the owner changed; everything
else uses the default below. Reading never fails: a missing table or bad value falls back to
the default, so a settings problem can never lock anyone out or take the site down.

Adding a control: add it to CONTROLS, then read it with `value("key")` where it applies.
Nothing else is needed for it to appear on the settings page with its change history.
"""
import json
from dataclasses import dataclass

from app import business_settings as store

SETTING_KEY = "security_controls"


@dataclass(frozen=True)
class Control:
    key: str
    group: str
    title: str
    help: str
    kind: str = "bool"          # bool, number, text, list
    default: object = True
    low: int = 0
    high: int = 0
    unit: str = ""
    risky_off: bool = False     # turning it off weakens protection: needs a confirm tick
    recommended: object = None  # shown when it differs from the default


@dataclass(frozen=True)
class FixedProtection:
    group: str
    title: str
    why_fixed: str


GROUPS = (
    "Staff sign-in",
    "Website forms",
    "Browser protections",
    "Customer pages and privacy",
    "Website content",
)

DEFAULT_PRIVACY = """Imperial Auto Care LLC ("we") collects the information you give us when you book, request an estimate, ask for roadside help or check a repair status: your name, phone number, email, vehicle details (including VIN and mileage), the location you give for mobile or roadside work, and what you tell us about the problem.

We use it only to answer your request, schedule and perform the work, send estimates, approvals, invoices and status updates you ask for, keep repair and tax records, and protect the shop against fraud.

We share it only with the service providers that run our systems (website hosting, our business phone and texting provider, payment processors and our accounting software), with parts suppliers when needed for your repair, and when the law requires it. We do not sell your information or share it for others' advertising.

Text messages: we text you about your own vehicle and requests. Message and data rates may apply. Reply STOP to stop texts at any time. We do not send marketing texts unless you separately agree.

We keep records for as long as needed for your vehicle's service history, warranty and tax purposes. You can ask to see, correct or delete your information by calling (480) 914-4144 or emailing jaelan@imperialautocarellc.com. Some records must be kept by law.

Private links to your repair status, estimate or invoice are meant for you only. Please do not share them.

Questions: Imperial Auto Care LLC, 7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307."""

DEFAULT_TERMS = """Estimates. Every repair starts with a written estimate. We do not start work you have not approved, and you approve or decline each line. If we find something else, we stop and ask before doing more. Estimates are good for 14 days.

Diagnostics. Diagnostic time is charged at the posted rate whether or not you go ahead with the repair. A basic screening is not a full diagnosis.

Bookings and requests. An online booking reserves a time for the listed service. An estimate request, roadside request or towing request is not a booking or a dispatch until we confirm it with you.

Deposits. Jobs over $1,000 need a 50% deposit before parts are ordered. Jobs from $500 to $1,000 may need a card on file.

Customer-supplied parts. We install customer-supplied parts at the customer-supplied-parts labor rate and do not warranty those parts or labor caused by their failure.

Payment. Payment is due when the vehicle is picked up. We accept Zelle, cash, card and the other methods listed at the counter. Financing may be available through Snap Finance.

Storage. Storage of $25 per day starts 3 days after we tell you the work is complete.

Warranty. Any warranty is stated on your invoice. If no warranty is stated, none is given beyond what the law requires.

Questions or a problem with our work: call (480) 914-4144."""

CONTROLS: tuple[Control, ...] = (
    # Staff sign-in
    Control("login_max_failures", "Staff sign-in", "Wrong passwords before a login is locked",
            "After this many wrong tries for one login or one internet address, sign-in is locked for the time below.",
            "number", 5, 3, 20, "tries"),
    Control("login_lock_minutes", "Staff sign-in", "How long a locked login stays locked",
            "Slows down anyone guessing passwords.", "number", 15, 5, 240, "minutes"),
    Control("session_hours", "Staff sign-in", "Sign staff out after",
            "How long someone stays signed in before they must sign in again (and enter their two-step code).",
            "number", 168, 1, 720, "hours", recommended=72),
    Control("website_login_button", "Staff sign-in", "Staff login button on the website",
            "Shows a small Staff login link in the website header and footer so you and staff can sign in from the site.",
            "bool", True),
    # Website forms
    Control("form_hourly_limit", "Website forms", "Website form sends per visitor per hour",
            "Bookings, estimate and roadside requests from one internet address. Stops spam and flooding.",
            "number", 5, 1, 50, "per hour"),
    Control("status_hourly_limit", "Website forms", "Status lookups per visitor per hour",
            "Stops someone guessing references or phone numbers to find other customers' jobs.",
            "number", 20, 5, 100, "per hour"),
    Control("block_cross_site_writes", "Website forms", "Block forms sent from other websites",
            "Refuses any save that a browser says started on another website (cross-site request forgery).",
            "bool", True, risky_off=True),
    Control("require_origin_header", "Website forms", "Extra strict: refuse saves with no origin",
            "Also refuses saves that don't say where they came from. Modern browsers always say; turn off if an old device has trouble saving.",
            "bool", False, recommended=True),
    # Browser protections
    Control("hsts", "Browser protections", "Always use a secure (https) connection",
            "Tells browsers to only ever connect securely, so nobody on public Wi-Fi can read or change pages.",
            "bool", True, risky_off=True),
    Control("content_security_policy", "Browser protections", "Only run code from approved sources",
            "Blocks scripts, frames and form posts to sites we don't use, which limits injected-code attacks.",
            "bool", True, risky_off=True),
    Control("block_framing", "Browser protections", "Stop other sites showing our pages inside theirs",
            "Prevents click-jacking. The TikTok LIVE overlay is always allowed.",
            "bool", True, risky_off=True),
    Control("permissions_policy", "Browser protections", "Turn off camera, microphone and payment access in pages",
            "Pages can't ask for the camera, microphone or browser payment sheet. Location stays allowed for roadside requests.",
            "bool", True),
    # Customer pages and privacy
    Control("status_link_days_after_pickup", "Customer pages and privacy", "Customer status links stop working after pickup",
            "Days after a job is picked up before its private status, approval and invoice link stops opening. 0 means never.",
            "number", 0, 0, 3650, "days", recommended=90),
    Control("privacy_notice_published", "Customer pages and privacy", "Show the privacy notice on the website",
            "Adds a Privacy link in the footer and next to every form. Review the wording below first.",
            "bool", False, recommended=True),
    Control("privacy_notice_text", "Customer pages and privacy", "Privacy notice wording",
            "What customers see on /site/privacy. Edit any time.", "text", DEFAULT_PRIVACY),
    Control("terms_published", "Customer pages and privacy", "Show service terms on the website",
            "Adds a Terms link in the footer and on booking and estimate pages.", "bool", False, recommended=True),
    Control("terms_text", "Customer pages and privacy", "Service terms wording",
            "What customers see on /site/terms. Edit any time.", "text", DEFAULT_TERMS),
    # Website content
    Control("hidden_services", "Website content", "Services hidden from the website",
            "One per line, using the service's short name. Hidden services disappear from menus, pages and the sitemap.",
            "list", ("nvzn-customs", "rentals")),
)

FIXED: tuple[FixedProtection, ...] = (
    FixedProtection("Staff sign-in", "Passwords stored only as bcrypt hashes",
                    "Nobody, including us, can read a password. There's nothing safe about turning this off."),
    FixedProtection("Staff sign-in", "Two-step login codes and recovery codes",
                    "Managed on your own Security page for each login, not here."),
    FixedProtection("Staff sign-in", "Role limits (tech, office, admin, owner)",
                    "Managed under Staff logins. Finance and logins stay owner-only."),
    FixedProtection("Customer pages and privacy", "Private customer pages are never cached or shown in search",
                    "Turning this off would let Google or a shared computer keep a customer's estimate or invoice."),
    FixedProtection("Customer pages and privacy", "Customer pages never show internal notes, supplier cost or other customers",
                    "Built into the pages themselves."),
    FixedProtection("Website forms", "Quo texts and calls only accepted with Quo's signature",
                    "Without it anyone could fake a customer text into the system."),
    FixedProtection("Website forms", "Website requests are never texted automatically",
                    "Staff contact website customers after review."),
    FixedProtection("Customer pages and privacy", "Nightly verified backups with an off-site copy",
                    "Failures text you. Managed with the database, not here."),
)

BY_KEY = {c.key: c for c in CONTROLS}


def _saved() -> dict:
    try:
        data = json.loads(store.raw(SETTING_KEY) or "{}")
    except (ValueError, KeyError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce(control: Control, raw):
    try:
        if control.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "on", "yes")
        if control.kind == "number":
            number = int(raw)
            return number if control.low <= number <= control.high else control.default
        if control.kind == "text":
            text = str(raw)
            return text if text.strip() else control.default
        if control.kind == "list":
            items = raw if isinstance(raw, (list, tuple)) else str(raw).splitlines()
            return tuple(dict.fromkeys(s.strip().lower() for s in items if str(s).strip()))
    except (TypeError, ValueError):
        pass
    return control.default


def value(key: str):
    """The owner's saved value, or the default. Never raises."""
    control = BY_KEY[key]
    saved = _saved()
    return _coerce(control, saved[key]) if key in saved else control.default


def values() -> dict:
    saved = _saved()
    return {c.key: (_coerce(c, saved[c.key]) if c.key in saved else c.default) for c in CONTROLS}


class Invalid(ValueError):
    pass


def parse(form) -> dict:
    """Owner form -> only the values that differ from the defaults. Raises Invalid with a plain message."""
    current = values()
    result = {}
    for c in CONTROLS:
        if c.kind == "bool":
            new = form.get(c.key) == "on"
            if c.risky_off and current[c.key] and not new and form.get(f"confirm_{c.key}") != "on":
                raise Invalid(f"Tick the confirm box to turn off \"{c.title}\". It lowers protection.")
        elif c.kind == "number":
            text = str(form.get(c.key, "")).strip()
            if not text.isdigit() or not c.low <= int(text) <= c.high:
                raise Invalid(f"{c.title}: enter a whole number from {c.low} to {c.high}.")
            new = int(text)
        elif c.kind == "text":
            new = str(form.get(c.key, "")).replace("\r\n", "\n").strip()
            if len(new) > 20000:
                raise Invalid(f"{c.title} is too long (20,000 characters max).")
            if not new:
                new = c.default
        else:
            new = _coerce(c, form.get(c.key, ""))
        if new != c.default:
            result[c.key] = list(new) if isinstance(new, tuple) else new
    return result


def save(db, form_values: dict, actor: str) -> bool:
    """Caller commits. Returns False when nothing changed."""
    return store.save(db, SETTING_KEY, form_values, actor)


def page_rows() -> list[dict]:
    current = values()
    rows = []
    for group in GROUPS:
        controls = [{"c": c, "value": current[c.key],
                     "display": "\n".join(current[c.key]) if c.kind == "list" else current[c.key],
                     "changed": current[c.key] != c.default}
                    for c in CONTROLS if c.group == group]
        fixed = [f for f in FIXED if f.group == group]
        rows.append({"group": group, "controls": controls, "fixed": fixed})
    return rows


# ── Browser headers ──────────────────────────────────────────────────────────
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    "media-src 'self' blob: https:",
    "connect-src 'self' https://vpic.nhtsa.dot.gov",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
))


def apply_headers(response, path: str, secure: bool) -> None:
    """Adds the protections the owner has on. Existing headers set by a route win."""
    current = values()
    overlay = path.startswith("/live/overlay")
    if current["hsts"] and secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if current["content_security_policy"]:
        frame = "frame-ancestors *" if overlay else "frame-ancestors 'none'"
        response.headers.setdefault("Content-Security-Policy", f"{CSP}; {frame}")
    if current["block_framing"] and not overlay:
        response.headers.setdefault("X-Frame-Options", "DENY")
    if current["permissions_policy"]:
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), payment=(), usb=(), geolocation=(self), interest-cohort=()")


def link_expired(ro, now=None) -> bool:
    """True when a picked-up job's private customer link is past the owner's limit (0 = never)."""
    from datetime import datetime, timedelta, timezone
    days = value("status_link_days_after_pickup")
    if not days or getattr(ro, "status", None) != "delivered" or not getattr(ro, "updated_at", None):
        return False
    changed = ro.updated_at if ro.updated_at.tzinfo else ro.updated_at.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - changed > timedelta(days=days)
