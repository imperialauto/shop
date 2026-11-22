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
Never contacts a customer. The caller commits.
"""
import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.dispatch import MAKES
from app.dispatch_model import DispatchAudit
from app.models import Customer

SOURCE = "quo_contact"
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


def apply(db: Session, contact: dict) -> dict:
    """contact: {id, firstName, lastName, company, phones: [..]}. Returns what changed."""
    name = person_name(contact.get("firstName"), contact.get("lastName"), contact.get("company"))
    if name is None:
        return {"updated": 0, "reason": "not_a_name"}
    phones = {digits(p) for p in contact.get("phones") or [] if len(digits(p)) == 10}
    if not phones:
        return {"updated": 0, "reason": "no_phone"}
    first, last = name
    updated, skipped = 0, []
    for customer in db.query(Customer).filter(Customer.phone.isnot(None)).all():
        if digits(customer.phone) not in phones:
            continue
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
    return {"updated": updated, "reason": None if updated else (skipped[0] if skipped else "no_customer")}
