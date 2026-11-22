"""A contact renamed in Quo renames the matching dashboard customer.

The messaging service gets Quo's contact webhook, reads the contact back from
Quo (nothing in the webhook body is trusted) and posts it here. Rules:
  - Only an existing customer with the same phone (last 10 digits) changes.
    Nobody is created and nothing is deleted.
  - A name typed in the dashboard (name_source "staff") is never replaced.
  - The shop often labels Quo contacts by job ("Honda Accord Oil Leak") and
    our own contact sync writes labels like that too. A label that doesn't
    read as a person's or business's name is ignored, so a job label never
    becomes a customer's name. That is also the loop guard: nothing here
    writes back to Quo, and the labels our sync writes are always refused.
An empty email on the customer is filled from Quo; an email already there stays.
The messaging service also sends every Quo contact once an hour (apply_all), so
a missed webhook or a contact renamed before this existed still catches up.

The other way (names_for_quo): a name typed in the dashboard is offered back
for Quo contacts that our own sync made (externalId "imperial-ops:...") and that
still carry a placeholder or job label. A contact made or named by hand in Quo
is never offered, and only when exactly one customer has that number. The
messaging service writes those, and only when QUO_NAME_WRITEBACK is on. It
cannot loop: the written name equals the dashboard's, which Quo-to-dashboard
already skips (typed names win), and a real name is never offered again.
Never contacts a customer. The caller commits.
"""
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.dispatch import MAKES
from app.dispatch_model import DispatchAudit
from app.models import Customer

SOURCE = "quo_contact"
OWN_PREFIX = "imperial-ops:"
MAX_TO_QUO = 50
WORD = re.compile(r"^[A-Za-z][A-Za-z'’.&-]*$")
MAKE = re.compile(rf"\b({MAKES}|chevy|mercedes-benz|benz|sprinter|f-?\d{{3}}|silverado|tahoe|camry|accord|civic|altima|explorer|impala)\b", re.I)
JOB = re.compile(
    r"\b(new|lead|unknown|leak|oil|brake|brakes|engine|trans|transmission|diag|diagnostic|repair|service|tow|towing|"
    r"ac|a/c|coolant|battery|tire|tires|alternator|starter|belt|pump|turbo|head|gasket|quote|estimate|job|"
    r"check|light|noise|mobile|customer|tiktok|facebook|google|yelp|spam|shop|imperial|jaelan|jae)\b", re.I)


def digits(phone) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())[-10:]


def person_name(first: str, last: str, company: str) -> tuple[str, str] | None:
    """(first, last) when the Quo label reads as a name, else None."""
    first, last, company = ((v or "").strip() for v in (first, last, company))
    if not (first or last) and company:
        first, last = company, ""
    full = f"{first} {last}".strip()
    words = full.split()
    if not 1 <= len(words) <= 4 or len(full) > 60:
        return None
    if not all(WORD.match(w) for w in words) or MAKE.search(full) or JOB.search(full):
        return None
    if not first:
        first, last = last, ""
    return first[:64], last[:64]


EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,120}\.[A-Za-z]{2,}$")


def _index(db: Session) -> dict:
    out = {}
    for customer in db.query(Customer).filter(Customer.phone.isnot(None)).all():
        out.setdefault(digits(customer.phone), []).append(customer)
    return out


def apply(db: Session, contact: dict, index: dict | None = None) -> dict:
    """contact: {id, firstName, lastName, company, emails, phones}. Returns what changed."""
    index = _index(db) if index is None else index
    phones = {digits(p) for p in contact.get("phones") or [] if len(digits(p)) == 10}
    customers = [c for p in phones for c in index.get(p, [])]
    if not phones:
        return {"updated": 0, "reason": "no_phone"}
    email = next((e.strip() for e in contact.get("emails") or [] if EMAIL.match((e or "").strip())), None)
    emailed = 0
    for customer in customers:
        if email and not (customer.email or "").strip():
            customer.email = email[:128]
            emailed += 1
    name = person_name(contact.get("firstName"), contact.get("lastName"), contact.get("company"))
    if name is None:
        db.flush()
        return {"updated": 0, "emails": emailed, "reason": "not_a_name"}
    if not customers:
        return {"updated": 0, "reason": "no_customer"}
    first, last = name
    updated, skipped = 0, []
    for customer in customers:
        if customer.name_source == "staff":
            skipped.append("typed_in_dashboard")
            continue
        if (customer.first_name or "", customer.last_name or "") in ((first, last), (first, last or "-")):
            skipped.append("same")
            continue
        before = f"{customer.first_name or ''} {customer.last_name or ''}".strip()
        customer.first_name, customer.last_name = first, last or "-"
        customer.name_source = SOURCE
        note = f"Name changed from \"{before}\" to match Quo on {datetime.now().strftime('%b %-d, %Y')}."
        customer.notes = f"{customer.notes}\n{note}" if customer.notes else note
        db.add(DispatchAudit(item_id=None, actor="quo", action="customer_renamed",
                             detail=f"customer {customer.id} from Quo contact {str(contact.get('id') or '')[:40]}"))
        updated += 1
    db.flush()
    return {"updated": updated, "emails": emailed, "reason": None if updated else (skipped[0] if skipped else "no_customer")}


def name_for_quo(contact: dict, index: dict) -> dict | None:
    """The dashboard's typed name for one of our own placeholder contacts, else None."""
    if not str(contact.get("externalId") or "").startswith(OWN_PREFIX) or not contact.get("id"):
        return None
    if person_name(contact.get("firstName"), contact.get("lastName"), contact.get("company")) is not None:
        return None  # already a real name in Quo: leave it
    phones = {digits(p) for p in contact.get("phones") or [] if len(digits(p)) == 10}
    customers = {c.id: c for p in phones for c in index.get(p, [])}
    if len(customers) != 1:
        return None
    customer = next(iter(customers.values()))
    if customer.name_source != "staff":
        return None
    last = "" if (customer.last_name or "").strip() == "-" else customer.last_name
    name = person_name(customer.first_name, last, "")
    if name is None:
        return None
    return {"id": str(contact["id"])[:64], "firstName": name[0], "lastName": name[1], "customer": customer.id}


def apply_all(db: Session, contacts: list[dict]) -> dict:
    """The hourly catch-up: every Quo contact at once, one customer lookup."""
    index = _index(db)
    renamed = emails = 0
    to_quo = []
    for contact in contacts:
        result = apply(db, contact, index)
        renamed += result.get("updated", 0)
        emails += result.get("emails", 0)
        offer = name_for_quo(contact, index)
        if offer and len(to_quo) < MAX_TO_QUO:
            to_quo.append(offer)
    return {"contacts": len(contacts), "renamed": renamed, "emails": emails, "names_for_quo": to_quo}
