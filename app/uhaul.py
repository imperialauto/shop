"""U-Haul work in the dashboard: requests in, jobs out, a bill-out sheet, and payments matched.

U-Haul has no API, EDI, emailed billing or bulk upload (checked Nov 27, 2026), so this module does every
step around the vendor portal and leaves the two steps that act at U-Haul to the owner:

1. Intake. A small Google Apps Script in the business mailbox posts each new U-Haul email to
   /webhooks/uhaul (see docs/uhaul-intake.md). Three kinds arrive:
   - roadside offers ("U-Haul Customer NAME needs roadside assistance!") with one link to U-Haul's
     roadside site, re-sent about 2 hours later in the same thread;
   - repair POs from repair dispatch (RPO number, equipment, estimated amount, reason down);
   - "Needs Review" resends of an RPO (bill it out or it will be voided).
   Re-sends never make a second card: they bump the one already there.
2. Alert. A new request shows on Home and the U-Haul page. A roadside offer has an Open offer button to
   U-Haul's own page; accepting or passing there is the owner's tap. After 15 minutes with no answer
   it shows as missed (U-Haul gives it to someone else). Nothing here accepts, bids or declines at U-Haul.
3. Job. "I took it" opens a fleet job for U-Haul with the job's PO (IAC-PO-...), and the request then
   moves En route, On site, Done, Back at shop, each stamped with the time, for port-to-port billing.
4. Bill-out sheet. Built from the job's lines, parts receipts and those times, in U-Haul's layout:
   line 1 travel (labor code 91, hours OR miles, never both), then on-site labor, then parts, plus
   condition, cause and correction once. Anything U-Haul would bounce is flagged before it's ready.
5. Submit. The sheet is what gets typed into the portal's Vendor RPO form. Marking it submitted is
   the owner's tap; nothing here submits.
6. Payments. A paid line copied from the portal's RPO History is matched by RPO number and compared
   with what was billed; a short pay is flagged.

The tables are new and separate (uhaul_requests, uhaul_payments). The app does not create tables on
start: until migrations/20261127_uhaul_flow.sql is applied and UHAUL_ENABLED=true is set, the U-Haul page
says it isn't switched on,
the webhook answers 503 so the mailbox script tries again later, and every other page works as before.
Nothing here texts, calls or emails anyone.
"""
import hmac
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, relationship

from app import po_numbers
from app.database import Base

OFFICE = ("owner", "admin", "office")
STAFF = OFFICE + ("tech",)
MISSED_AFTER = timedelta(minutes=15)  # an unanswered roadside offer has gone to another vendor by then
RESEND_WINDOW = timedelta(hours=6)    # the same customer's offer again within this is a re-send
MAX_BODY = 60_000

STAGES = ("new", "accepted", "en_route", "on_site", "done", "back", "ready", "submitted", "paid")
CLOSED = ("declined", "missed", "paid", "cancelled")
STAGE_LABEL = {
    "new": "New", "accepted": "Taken", "en_route": "En route", "on_site": "On site", "done": "Work done",
    "back": "Back at shop", "ready": "Ready to submit", "submitted": "Submitted", "paid": "Paid",
    "declined": "Passed", "missed": "Missed", "cancelled": "Cancelled",
}
# The tap that moves a job on, from each stage. Done and Back at shop stamp the times U-Haul bills on.
NEXT_STEP = {"accepted": ("en_route", "Leaving now"), "en_route": ("on_site", "On site"),
             "on_site": ("done", "Work done"), "done": ("back", "Back at shop")}
TIME_FIELD = {"en_route": "en_route_at", "on_site": "on_site_at", "done": "done_at", "back": "back_at"}
KIND_LABEL = {"roadside": "Roadside", "rpo": "Repair PO"}

# U-Haul labor codes seen on Imperial's own bill-outs and in U-Haul's vendor guide. Rates come from the
# shop's FMA rate tab in the portal and can't be typed, so the sheet carries codes and hours only.
TRAVEL_CODE = "91"
TRAVEL_LABEL = "Emergency Aid Travel Time"
CODE_HINTS = (
    (("tow", "hook"), "90 / 92", "Tow (hook fee 90)"),
    (("jump", "battery", "boost", "alternator", "starter", "electrical", "light", "fuse"), "Group 10", "Electrical"),
    (("tire", "flat", "wheel"), "1-131", "Tire"),
)


class UhaulRequest(Base):
    """One U-Haul request: a roadside offer or a repair PO, from the email to the payment."""
    __tablename__ = "uhaul_requests"
    id = Column(Integer, primary_key=True)
    kind = Column(String(16), nullable=False)  # roadside, rpo
    status = Column(String(16), nullable=False, default="new")
    message_id = Column(String(200), unique=True)
    thread_id = Column(String(200), index=True)
    sender = Column(String(200))
    subject = Column(String(300))
    received_at = Column(DateTime(timezone=True))
    customer_name = Column(String(128))  # the U-Haul customer on a roadside offer
    offer_url = Column(String(500))      # U-Haul's own roadside page, opened by the owner
    rpo_number = Column(String(32), index=True)
    hotline_ref = Column(String(32))
    equipment_number = Column(String(48))
    vin = Column(String(17))
    plate = Column(String(32))
    year_make = Column(String(80))
    location = Column(Text)
    action_date = Column(Date)
    repair_by = Column(Date)
    estimated_amount = Column(Float)
    reason = Column(Text)
    cause_text = Column(String(200))
    rdm_email = Column(String(128))
    review_reason = Column(Text)  # U-Haul's words on its latest "Needs Review" resend
    resend_count = Column(Integer, nullable=False, default=0)
    last_seen_at = Column(DateTime(timezone=True))
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id", ondelete="SET NULL"), index=True)
    decided_at = Column(DateTime(timezone=True))
    decided_by = Column(String(64))
    en_route_at = Column(DateTime(timezone=True))
    on_site_at = Column(DateTime(timezone=True))
    done_at = Column(DateTime(timezone=True))
    back_at = Column(DateTime(timezone=True))
    odometer = Column(Integer)
    is_trailer = Column(Boolean, nullable=False, default=False)
    travel_basis = Column(String(8), nullable=False, default="hours")  # hours, miles (U-Haul pays one, not both)
    travel_miles = Column(Float)
    condition = Column(Text)
    cause = Column(Text)
    correction = Column(Text)
    vendor_remarks = Column(Text)
    ready_at = Column(DateTime(timezone=True))
    submitted_at = Column(DateTime(timezone=True))
    submitted_by = Column(String(64))
    submitted_invoice = Column(String(32))
    submitted_amount = Column(Float)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    repair_order = relationship("RepairOrder")
    payments = relationship("UhaulPayment", back_populates="request", order_by="UhaulPayment.id")


class UhaulPayment(Base):
    """A paid line from the portal's RPO History (status VNPAID), matched to its request by RPO number."""
    __tablename__ = "uhaul_payments"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("uhaul_requests.id", ondelete="SET NULL"), index=True)
    rpo_number = Column(String(32), nullable=False, index=True)
    paid_on = Column(Date)
    reference = Column(String(64))  # check or payment reference shown in RPO History
    amount = Column(Float, nullable=False)
    recorded_by = Column(String(64))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    request = relationship("UhaulRequest", back_populates="payments")


# ---------------------------------------------------------------- switched on?

_ready: dict = {}


def enabled() -> bool:
    """UHAUL_ENABLED=true switches the U-Haul page on. It stays off until the migration is applied."""
    return os.getenv("UHAUL_ENABLED", "").strip().lower() == "true"


def ready(db: Session) -> bool:
    """True once switched on and both tables exist. A missing table is checked again a minute later."""
    if not enabled():
        return False
    bind = db.get_bind()
    at, ok = _ready.get(id(bind), (0.0, False))
    if ok or (at and time.monotonic() - at < 60):
        return ok
    try:
        checker = inspect(bind)
        ok = checker.has_table("uhaul_requests") and checker.has_table("uhaul_payments")
    except SQLAlchemyError:
        ok = False
    _ready[id(bind)] = (time.monotonic(), ok)
    return ok


def intake_key() -> str:
    return os.getenv("UHAUL_INTAKE_KEY", "").strip()


def key_ok(given: str | None) -> bool:
    """The mailbox script's key. With no key set the webhook is off, so nobody can post jobs in."""
    expected = intake_key()
    return len(expected) >= 24 and bool(given) and hmac.compare_digest(given.strip(), expected)


# ---------------------------------------------------------------- reading the emails

_ADDR = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_RPO = re.compile(r"\b(\d{3}-\d{3,6}[A-Z]{1,3})\b")
_ROADSIDE_SUBJECT = re.compile(r"U-?Haul\s+Customer\s+(.+?)\s+needs\s+roadside", re.IGNORECASE)
_LINK = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)


def sender_address(raw: str | None) -> str:
    found = _ADDR.findall(raw or "")
    return found[-1].lower() if found else ""


def from_uhaul(raw: str | None) -> bool:
    """Only U-Haul's own domains, so a look-alike mail can't open a job."""
    addr = sender_address(raw)
    domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
    return domain == "uhaul.com" or domain.endswith(".uhaul.com")


def safe_offer_url(url: str | None) -> str:
    """The offer link is shown only when it is https on U-Haul's own domains."""
    url = (url or "").strip().rstrip(".,;")
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    ok_host = any(host == d or host.endswith("." + d) for d in ("uhaul.com", "uhaul.net"))
    if parts.scheme != "https" or not ok_host or parts.username or parts.password or parts.port not in (None, 443):
        return ""
    return url[:500]


def _clean(text: str) -> str:
    """Plain text from Gmail's plain body, which may carry markdown stars and odd spacing."""
    return (text or "").replace("\r", "").replace("*", "").replace(" ", " ")


def _field(text: str, *labels: str) -> str:
    for label in labels:
        m = re.search(rf"(?im)^[ \t\-•]*{label}[ \t]*[:#]?[ \t]*:?[ \t]*(\S.*?)[ \t]*$", text)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return ""


def _date(value: str) -> date | None:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", value or "")
    if not m:
        return None
    try:
        d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None
    return None if d.year < 2000 else d


def _money(value: str) -> float | None:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)", value or "")
    if not m:
        return None
    try:
        return round(float(m.group(1).replace(",", "")), 2)
    except ValueError:
        return None


def _between(text: str, start: str, ends: tuple[str, ...]) -> str:
    m = re.search(start, text, re.IGNORECASE)
    if not m:
        return ""
    rest = text[m.end():]
    cut = len(rest)
    for end in ends:
        e = re.search(end, rest, re.IGNORECASE)
        if e:
            cut = min(cut, e.start())
    lines = [ln.strip(" \t-•") for ln in rest[:cut].splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def parse(subject: str | None, body: str | None, links: list | None = None) -> dict | None:
    """What a U-Haul email says, or None when it's some other U-Haul email (COI reminders and the like)."""
    subject = (subject or "").strip()
    text = _clean(body or "")[:MAX_BODY]
    roadside = _ROADSIDE_SUBJECT.search(subject)
    if roadside:
        candidates = list(links or []) + _LINK.findall(text)
        url = next((u for u in (safe_offer_url(c) for c in candidates) if u), "")
        return {"kind": "roadside", "customer_name": roadside.group(1).strip()[:128], "offer_url": url}

    rpo = (_RPO.search(_field(text, r"RPO", r"Repair\s+PO", r"U-Haul\s+Repair\s+Purchase\s+Order"))
           or _RPO.search(subject))
    if not rpo:
        return None
    info: dict = {"kind": "rpo", "rpo_number": rpo.group(1).upper()}
    review = re.search(r"resent to you because of the following reason\s*:?\s*(.+?)(?:\n\s*\n|U-Haul Repair Purchase Order)",
                       text, re.IGNORECASE | re.DOTALL)
    if review or "needs review" in subject.lower():
        info["review_reason"] = " ".join((review.group(1) if review else "U-Haul marked this PO Needs Review.").split())[:500]
    equipment = _field(text, r"Equipment\s*#", r"Equip")
    if equipment:
        info["equipment_number"] = " ".join(equipment.split())[:48]
    vin = re.search(r"\bVIN\s*#?\s*:?\s*([A-HJ-NPR-Z0-9]{17})\b", text, re.IGNORECASE)
    if vin:
        info["vin"] = vin.group(1).upper()
    plate = re.search(r"\bPlate\s*#?\s*:?\s*([A-Z0-9]{2,10})\b", text, re.IGNORECASE)
    if plate:
        info["plate"] = plate.group(1).upper()
    year_make = _field(text, r"Year\s*/\s*Make\s*/\s*Model", r"Year\s*/\s*Make")
    if not year_make:
        y, mk = _field(text, r"Year"), _field(text, r"Make")
        year_make = f"{y} / {mk}".strip(" /") if (y or mk) else ""
    if year_make:
        info["year_make"] = year_make[:80]
    amount = _field(text, r"Estimated\s+amount")
    if amount and _money(amount) is not None:
        info["estimated_amount"] = _money(amount)
    hotline = re.search(r"Hot\s*Line\s+Ref\s*#?\s*:?\s*(\d{4,})", text, re.IGNORECASE)
    if hotline:
        info["hotline_ref"] = hotline.group(1)
    for key, labels in (("action_date", (r"Action\s+Date", r"Issued\s+On")), ("repair_by", (r"Repair\s+By",))):
        found = re.search(rf"(?:{'|'.join(labels)})\s*:?\s*(\d{{1,2}}/\d{{1,2}}/\d{{4}})", text, re.IGNORECASE)
        if found and _date(found.group(1)):
            info[key] = _date(found.group(1))
    location = _between(text, r"(?:U-Haul\s+)?Location\s+Info", (r"Reason\s+Down", r"Repair\s+Dispatch\s+Contact"))
    if location:
        info["location"] = location[:600]
    reason = _between(text, r"Reason\s+Down", (r"Repair\s+Dispatch\s+Contact", r"Related\s+Photos", r"E-Vendor"))
    notes = _between(text, r"(?m)^\s*Notes\s*:", (r"Equipment\s+Info",))
    remarks = _field(text, r"Remarks")
    info["reason"] = "\n".join(p for p in (notes, reason, remarks) if p)[:2000] or None
    cause = _field(text, r"Cause")
    if cause:
        info["cause_text"] = cause[:200]
    rdm = re.search(r"reply to\s+([\w.+\-]+@uhaul\.com)", text, re.IGNORECASE)
    if rdm:
        info["rdm_email"] = rdm.group(1).lower()
    return info


# ---------------------------------------------------------------- intake

def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def receive(db: Session, payload: dict, now: datetime | None = None) -> tuple[str, UhaulRequest | None]:
    """Files one email. Returns (what happened, the request). Never acts at U-Haul and texts nobody.

    What happened is one of: new, resend, duplicate, ignored (not a U-Haul request or not from U-Haul)."""
    now = now or datetime.now(timezone.utc)
    if not from_uhaul(payload.get("from")):
        return "ignored", None
    message_id = str(payload.get("message_id") or "").strip()[:200] or None
    if message_id:
        seen = db.query(UhaulRequest).filter(UhaulRequest.message_id == message_id).first()
        if seen is not None:
            return "duplicate", seen
    info = parse(payload.get("subject"), payload.get("body"), payload.get("links"))
    if info is None:
        return "ignored", None
    received = _parse_when(payload.get("date")) or now
    thread_id = str(payload.get("thread_id") or "").strip()[:200] or None

    existing = None
    if info["kind"] == "roadside":
        if thread_id:
            existing = (db.query(UhaulRequest).filter(UhaulRequest.kind == "roadside", UhaulRequest.thread_id == thread_id)
                        .order_by(UhaulRequest.id.desc()).first())
        if existing is None and info.get("customer_name"):
            recent = (db.query(UhaulRequest).filter(UhaulRequest.kind == "roadside",
                                                    UhaulRequest.customer_name == info["customer_name"])
                      .order_by(UhaulRequest.id.desc()).first())
            if recent is not None and received - (_aware(recent.received_at) or received) <= RESEND_WINDOW:
                existing = recent
    else:
        existing = (db.query(UhaulRequest).filter(UhaulRequest.rpo_number == info["rpo_number"])
                    .order_by(UhaulRequest.id.desc()).first())
        if existing is None and info.get("hotline_ref"):
            # A roadside job that was requested in the portal comes back as an RPO carrying the hotline number.
            existing = (db.query(UhaulRequest).filter(UhaulRequest.hotline_ref == info["hotline_ref"])
                        .order_by(UhaulRequest.id.desc()).first())

    if existing is not None:
        existing.resend_count = (existing.resend_count or 0) + 1
        existing.last_seen_at = received
        if info.get("offer_url") and not existing.offer_url:
            existing.offer_url = info["offer_url"]
        for key, value in info.items():
            if key in ("kind", "customer_name", "offer_url") or value in (None, ""):
                continue
            if key == "review_reason" or getattr(existing, key, None) in (None, ""):
                setattr(existing, key, value)
        db.flush()
        return "resend", existing

    row = UhaulRequest(kind=info.pop("kind"), status="new", message_id=message_id, thread_id=thread_id,
                       sender=sender_address(payload.get("from"))[:200], subject=str(payload.get("subject") or "")[:300],
                       received_at=received, last_seen_at=received, **info)
    db.add(row)
    db.flush()
    return "new", row


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(when)


# ---------------------------------------------------------------- where each one stands

def shown_status(req: UhaulRequest, now: datetime | None = None) -> str:
    """A roadside offer nobody answered for 15 minutes reads as missed. Saved status stays new."""
    now = now or datetime.now(timezone.utc)
    if (req.status == "new" and req.kind == "roadside"
            and now - (_aware(req.received_at) or now) > MISSED_AFTER):
        return "missed"
    return req.status


def is_open(req: UhaulRequest, now: datetime | None = None) -> bool:
    return shown_status(req, now) not in CLOSED


def board(db: Session, now: datetime | None = None) -> dict:
    """What the U-Haul page and Home show: new requests, jobs under way, bill-outs to finish, money due."""
    now = now or datetime.now(timezone.utc)
    rows = db.query(UhaulRequest).order_by(UhaulRequest.id.desc()).limit(300).all()
    groups = {"new": [], "working": [], "billing": [], "submitted": [], "closed": []}
    for r in rows:
        s = shown_status(r, now)
        if s == "new":
            groups["new"].append(r)
        elif s in ("accepted", "en_route", "on_site"):
            groups["working"].append(r)
        elif s in ("done", "back", "ready"):
            groups["billing"].append(r)
        elif s == "submitted":
            groups["submitted"].append(r)
        else:
            groups["closed"].append(r)
    groups["closed"] = groups["closed"][:40]
    groups["needs_review"] = [r for r in rows if r.review_reason and shown_status(r, now) not in ("paid", "declined", "cancelled")]
    groups["owed"] = round(sum(owed(r) for r in groups["submitted"]), 2)
    groups["missed_month"] = sum(1 for r in rows if shown_status(r, now) == "missed"
                                 and now - (_aware(r.received_at) or now) <= timedelta(days=30))
    return groups


def home_summary(db: Session, now: datetime | None = None) -> dict | None:
    """The Home card, or None when there's nothing open (or the tables aren't there yet)."""
    try:
        if not ready(db):
            return None
        b = board(db, now)
    except SQLAlchemyError:
        db.rollback()
        return None
    if not (b["new"] or b["working"] or b["billing"] or b["needs_review"]):
        return None
    return {"new": b["new"][:3], "new_count": len(b["new"]), "working": b["working"][:3],
            "billing_count": len(b["billing"]), "review_count": len(b["needs_review"])}


# ---------------------------------------------------------------- taking it and moving it on

def _uhaul_customer(db: Session):
    from app.models import Customer
    found = (db.query(Customer).filter(Customer.customer_type == "fleet", Customer.fleet_name.ilike("u-haul%"))
             .order_by(Customer.id).first())
    if found is not None:
        return found
    found = Customer(customer_type="fleet", fleet_name="U-Haul", first_name="U-Haul", last_name="Fleet",
                     notes="Fleet account for U-Haul roadside and repair POs. Made by the U-Haul page.")
    db.add(found)
    db.flush()
    return found


def _vehicle_for(db: Session, req: UhaulRequest, customer):
    from app.models import Vehicle
    if req.vin:
        found = db.query(Vehicle).filter(Vehicle.vin == req.vin).first()
        if found is not None:
            return found
    year, make, model = None, None, None
    parts = [p.strip() for p in (req.year_make or "").split("/") if p.strip()]
    if parts and parts[0].isdigit():
        year = int(parts[0])
        parts = parts[1:]
    if parts:
        make = parts[0][:64]
    if len(parts) > 1:
        model = parts[1][:128]
    label = req.equipment_number or (f"customer {req.customer_name}" if req.customer_name else "")
    v = Vehicle(customer_id=customer.id, year=year, make=make or "U-Haul", model=model or "Equipment",
                vin=req.vin, license_plate=(req.plate or None) and req.plate[:16],
                notes=f"U-Haul equipment {label}".strip())
    db.add(v)
    db.flush()
    return v


def _concern(req: UhaulRequest) -> str:
    if req.kind == "roadside":
        head = f"U-Haul roadside call for {req.customer_name or 'a U-Haul customer'}"
    else:
        head = f"U-Haul repair PO {req.rpo_number}"
    bits = [head]
    if req.equipment_number:
        bits.append(f"Equipment {req.equipment_number}")
    if req.reason:
        bits.append(req.reason.splitlines()[0][:300])
    return ". ".join(bits)


def take(db: Session, req: UhaulRequest, who: str, now: datetime | None = None):
    """The owner took it (on U-Haul's page for a roadside offer). Opens the fleet job. Acts nowhere else."""
    from app.models import RepairOrder
    import secrets
    if req.status not in ("new",):
        raise ValueError("This one has already been answered.")
    now = now or datetime.now(timezone.utc)
    customer = _uhaul_customer(db)
    vehicle = _vehicle_for(db, req, customer)
    ro = RepairOrder(customer_id=customer.id, vehicle_id=vehicle.id, concern=_concern(req),
                     tech_notes="\n".join(x for x in (
                         f"U-Haul RPO {req.rpo_number}" if req.rpo_number else "",
                         f"Hotline ref {req.hotline_ref}" if req.hotline_ref else "",
                         f"Location: {' '.join(req.location.split())}" if req.location else "") if x) or None,
                     public_token=secrets.token_urlsafe(32))
    db.add(ro)
    db.flush()
    req.repair_order_id = ro.id
    req.status = "accepted"
    req.decided_at, req.decided_by = now, who[:64]
    if not req.condition and req.reason:
        req.condition = req.reason[:2000]
    if not req.cause and req.cause_text:
        req.cause = req.cause_text
    db.flush()
    return ro


def pass_on(db: Session, req: UhaulRequest, who: str, now: datetime | None = None) -> None:
    if req.status != "new":
        raise ValueError("This one has already been answered.")
    req.status = "declined"
    req.decided_at, req.decided_by = now or datetime.now(timezone.utc), who[:64]


def step(req: UhaulRequest, to: str, now: datetime | None = None) -> None:
    """En route, on site, done, back at shop: each one stamps its time. Only forward, one step at a time."""
    expected = NEXT_STEP.get(req.status)
    if expected is None or expected[0] != to:
        raise ValueError("That step doesn't come next for this job.")
    setattr(req, TIME_FIELD[to], now or datetime.now(timezone.utc))
    req.status = to


def skip_travel(req: UhaulRequest, now: datetime | None = None) -> None:
    """A repair PO done at the shop: no trip, so straight to on site with no travel line."""
    if req.status != "accepted":
        raise ValueError("Only a job that hasn't started can skip the trip.")
    req.on_site_at = now or datetime.now(timezone.utc)
    req.status = "on_site"


def undo_step(req: UhaulRequest) -> None:
    """A wrong tap: back one step and clear the time it stamped."""
    order = ["accepted", "en_route", "on_site", "done", "back"]
    if req.status not in order[1:]:
        raise ValueError("Nothing to undo.")
    setattr(req, TIME_FIELD[req.status], None)
    previous = order[order.index(req.status) - 1]
    if previous == "en_route" and req.en_route_at is None:
        previous = "accepted"
    req.status = previous


# ---------------------------------------------------------------- the bill-out sheet

def _hours(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    seconds = (_aware(end) - _aware(start)).total_seconds()
    return round(max(seconds, 0) / 3600, 1) if seconds >= 0 else None


def code_for(description: str) -> tuple[str, str]:
    text = (description or "").lower()
    for words, code, label in CODE_HINTS:
        if any(w in text for w in words):
            return code, label
    return "", "Look up in Labor Lookup"


def _receipts(db: Session, ro_id: int) -> list:
    try:
        from app import parts_receipts
        if not parts_receipts.ready(db):
            return []
        return db.query(parts_receipts.PartsReceipt).filter(parts_receipts.PartsReceipt.repair_order_id == ro_id).all()
    except (ImportError, SQLAlchemyError):
        db.rollback()
        return []


def _receipts_ready(db: Session) -> bool:
    try:
        from app import parts_receipts
        return parts_receipts.ready(db)
    except ImportError:
        return False


def sheet(db: Session, req: UhaulRequest) -> dict:
    """The Vendor RPO form, filled from the job. Flags say what U-Haul would bounce.

    A flag with block=True keeps it from being marked ready to submit."""
    from app.models import PartsOrder
    ro = req.repair_order
    flags: list[dict] = []

    def flag(text, block=True):
        flags.append({"text": text, "block": block})

    lines = [li for li in (ro.line_items if ro else []) if li.billable and li.item_type != "discount"]
    labor = [li for li in lines if li.item_type in ("labor", "sublet", "misc")]
    parts = [li for li in lines if li.item_type == "part"]
    orders = (db.query(PartsOrder).filter(PartsOrder.repair_order_id == ro.id).all() if ro else [])
    receipts = _receipts(db, ro.id) if ro else []

    rows = []
    out_h = _hours(req.en_route_at, req.on_site_at)
    in_h = _hours(req.done_at, req.back_at)
    onsite_h = _hours(req.on_site_at, req.done_at)
    if req.en_route_at:
        if req.travel_basis == "miles":
            if not req.travel_miles:
                flag("Travel is set to miles. Add the round-trip miles.")
            rows.append({"line": 1, "type": "Labor", "code": TRAVEL_CODE, "what": f"{TRAVEL_LABEL} (miles)",
                         "qty": req.travel_miles or 0, "unit": "miles", "cause": "Travel to and from the unit"})
        else:
            if out_h is None or in_h is None:
                flag("Travel times aren't complete. Tap Back at shop when you're back, or fix the times.")
            travel = round((out_h or 0) + (in_h or 0), 1)
            if out_h is not None and in_h is not None and travel <= 0:
                flag("Travel comes out to 0 hours. Check the leave, arrive, done and back times.")
            rows.append({"line": 1, "type": "Labor", "code": TRAVEL_CODE, "what": f"{TRAVEL_LABEL} (port to port)",
                         "qty": travel, "unit": "hours", "cause": "Travel to and from the unit"})
    elif req.kind == "roadside":
        flag("No leave time. Roadside travel is billed port to port, so U-Haul needs when you left and got back.")

    if labor:
        for li in labor:
            code, label = code_for(li.description)
            rows.append({"line": len(rows) + 1, "type": "Labor", "code": code or "?", "what": li.description,
                         "qty": round(li.quantity or 0, 2), "unit": "hours", "cause": label,
                         "job_amount": round(li.total, 2)})
            if not code:
                flag(f"Pick the labor code for “{li.description[:60]}” in the portal's Labor Lookup.", block=False)
    elif onsite_h:
        rows.append({"line": len(rows) + 1, "type": "Labor", "code": "?", "what": "On-site labor", "qty": onsite_h,
                     "unit": "hours", "cause": "Look up in Labor Lookup"})
        flag("No labor lines on the job. On-site time is shown instead; add the labor line to the job.", block=False)
    else:
        flag("No labor on the job yet.")

    for li in parts:
        match = next((o for o in orders if o.description and li.description
                      and (o.description.lower() in li.description.lower() or li.description.lower() in o.description.lower())), None)
        part_no = (match.part_number if match else "") or ""
        cost = match.cost if match and match.cost else None
        rows.append({"line": len(rows) + 1, "type": "Parts", "code": "", "what": li.description,
                     "qty": round(li.quantity or 0, 2), "unit": "each", "part_number": part_no,
                     "unit_cost": cost, "job_amount": round(li.total, 2)})
        if not part_no:
            flag(f"Part number missing for “{li.description[:60]}”. Add it on the job's Parts card.")
    if parts and _receipts_ready(db) and not receipts:
        flag("Parts are on the job but no receipt is saved. U-Haul needs the receipt uploaded with the bill-out.")

    if not req.rpo_number:
        flag("No RPO number yet. Request one in the portal (Request RPO) with the hotline number, then type it here."
             if req.kind == "roadside" else "No RPO number on this request.")
    if req.odometer is None and not req.is_trailer:
        flag("Odometer missing. Use 0 and tick Trailer for a trailer.")
    if not (req.correction or "").strip():
        flag("Correction is empty. Say what was done.")
    if not (req.condition or "").strip():
        flag("Condition is empty. Say what was wrong.")
    job_total = round(sum(li.total for li in lines), 2)
    if req.estimated_amount and job_total > req.estimated_amount + 0.01:
        flag(f"The job comes to ${job_total:,.2f}, over U-Haul's ${req.estimated_amount:,.2f} estimate. "
             f"Get the increase approved by repair dispatch before billing.", block=False)

    remarks = req.vendor_remarks or default_remarks(req)
    invoice_number = default_invoice(ro)
    finish = req.done_at or req.back_at
    start = req.on_site_at or req.en_route_at or req.decided_at
    return {
        "rows": rows, "flags": flags, "blocked": any(f["block"] for f in flags),
        "header": {
            "RPO number": req.rpo_number or "—",
            "Vendor location state": "AZ",
            "Odometer": "0 (trailer)" if req.is_trailer else (f"{req.odometer:,}" if req.odometer is not None else "—"),
            "Start date": _local(start, "%m/%d/%Y") if start else "—",
            "Finish date": _local(finish, "%m/%d/%Y") if finish else "—",
            "Invoice #": invoice_number,
            "Invoice date": _local(finish, "%m/%d/%Y") if finish else "—",
        },
        "remarks": remarks,
        "condition": req.condition or "", "cause": req.cause or req.cause_text or "", "correction": req.correction or "",
        "job_total": job_total, "receipts": receipts, "po": po_numbers.for_job(ro.id) if ro else "",
        "request_rpo": {
            "Breakdown / hotline #": req.hotline_ref or "—",
            "Equipment #": req.equipment_number or "—",
            "Mileage": "0" if req.is_trailer else (str(req.odometer) if req.odometer is not None else "—"),
            "Estimated repair amount": f"{job_total:,.2f}" if job_total else "—",
            "Invoice #": invoice_number,
            "Comments": (req.correction or req.condition or "").strip()[:250] or "—",
        },
    }


def default_invoice(ro) -> str:
    """The shop's invoice number for the job if it has one, else the job number."""
    if ro is None:
        return "—"
    inv = next((i for i in ro.invoices if i.status != "void"), None)
    return str(inv.id) if inv else str(ro.id)


def _local(when: datetime | None, fmt: str) -> str:
    from app.appointments import PHOENIX
    return _aware(when).astimezone(PHOENIX).strftime(fmt) if when else ""


def default_remarks(req: UhaulRequest) -> str:
    bits = []
    for label, when in (("Left shop", req.en_route_at), ("On site", req.on_site_at), ("Done", req.done_at),
                        ("Back at shop", req.back_at)):
        if when:
            bits.append(f"{label} {_local(when, '%m/%d %I:%M %p')}")
    if req.repair_order_id:
        bits.append(f"Job #{req.repair_order_id}, PO {po_numbers.for_job(req.repair_order_id)}")
    return ". ".join(bits)


def mark_ready(db: Session, req: UhaulRequest, now: datetime | None = None) -> None:
    if req.status not in ("done", "back"):
        raise ValueError("Finish the job first.")
    if sheet(db, req)["blocked"]:
        raise ValueError("Fix the items marked on the sheet first.")
    req.status = "ready"
    req.ready_at = now or datetime.now(timezone.utc)


def mark_submitted(db: Session, req: UhaulRequest, who: str, invoice: str, amount: float | None,
                   now: datetime | None = None) -> None:
    """The owner submitted it in the portal. This records that; it submits nothing."""
    if req.status != "ready":
        raise ValueError("Mark the sheet ready first.")
    req.status = "submitted"
    req.submitted_at = now or datetime.now(timezone.utc)
    req.submitted_by = who[:64]
    req.submitted_invoice = (invoice or "").strip()[:32] or None
    req.submitted_amount = amount
    req.review_reason = None


# ---------------------------------------------------------------- payments

def paid_total(req: UhaulRequest) -> float:
    return round(sum(p.amount or 0 for p in req.payments), 2)


def owed(req: UhaulRequest) -> float:
    billed = req.submitted_amount or 0
    return round(max(billed - paid_total(req), 0), 2) if req.status == "submitted" else 0.0


def short_paid(req: UhaulRequest) -> float:
    """How much less U-Haul paid than was billed, once any payment is in. 0 when none."""
    if not req.payments or not req.submitted_amount:
        return 0.0
    return round(max(req.submitted_amount - paid_total(req), 0), 2)


def record_payment(db: Session, rpo_number: str, amount: float, paid_on: date | None, reference: str, who: str):
    """A paid line from RPO History. Matched by RPO number; marks the request paid when it covers the bill."""
    rpo = (rpo_number or "").strip().upper()
    if not _RPO.fullmatch(rpo):
        raise ValueError("That doesn't look like a U-Haul RPO number (like 478-00508DA).")
    if amount is None or amount <= 0:
        raise ValueError("Enter the amount paid.")
    req = db.query(UhaulRequest).filter(UhaulRequest.rpo_number == rpo).order_by(UhaulRequest.id.desc()).first()
    ref = (reference or "").strip()[:64] or None
    dup = db.query(UhaulPayment).filter(UhaulPayment.rpo_number == rpo, UhaulPayment.amount == round(amount, 2),
                                        UhaulPayment.reference == ref).first()
    if dup is not None:
        raise ValueError("That payment is already recorded.")
    pay = UhaulPayment(request_id=req.id if req else None, rpo_number=rpo, amount=round(amount, 2),
                       paid_on=paid_on, reference=ref, recorded_by=who[:64])
    db.add(pay)
    db.flush()
    if req is not None:
        db.refresh(req)
        if req.status == "submitted" and req.submitted_amount and paid_total(req) >= req.submitted_amount - 0.01:
            req.status = "paid"
        elif req.status == "submitted" and not req.submitted_amount:
            req.status = "paid"
    return pay, req


def unmatched_payments(db: Session) -> list:
    return db.query(UhaulPayment).filter(UhaulPayment.request_id.is_(None)).order_by(UhaulPayment.id.desc()).limit(30).all()
