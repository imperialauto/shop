"""Customer records from Quo: saved contacts and everyone who has texted the shop.

Only adds. An existing customer with the same phone (last 10 digits) is never
changed or duplicated. The owner sees the full list and picks rows before
anything is saved.
"""
import os
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem
from app.models import Customer

SHOP_NUMBERS = {"4809144144"}
TIMEOUT = 90.0


def digits(phone) -> str:
    return "".join(c for c in str(phone or "") if c.isdigit())[-10:]


def skip_reason(phone: str) -> str | None:
    if len(phone) != 10:
        return "not a phone number"
    if phone in SHOP_NUMBERS or phone == digits(OWNER_PHONE):
        return "shop or owner number"
    if phone[:3] in ("800", "833", "844", "855", "866", "877", "888"):
        return "toll-free"
    return None


def fetch_directory():
    base = os.environ.get("MESSAGING_BRIDGE_URL", "").rstrip("/")
    token = os.environ.get("OPERATIONS_BRIDGE_TOKEN", "")
    if not base or not token:
        return None, "Texts aren't connected yet."
    try:
        r = httpx.get(f"{base}/api/operations/directory", headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None, "Quo couldn't be read right now. Try again in a minute."
        return r.json(), None
    except (httpx.HTTPError, ValueError):
        return None, "Quo couldn't be read right now. Try again in a minute."


def split_name(contact: dict) -> tuple[str, str, str | None]:
    first, last, company = (contact.get(k, "").strip() for k in ("firstName", "lastName", "company"))
    if first or last:
        return (first or last)[:64], (last if first else "")[:64], company or None
    if company:
        return company[:64], "", company
    return "", "", None


def preview(db: Session, directory: dict) -> list[dict]:
    """One row per new phone number, newest texter first. Nothing is saved."""
    known = {digits(c.phone) for c in db.query(Customer).all() if c.phone}
    rows: dict[str, dict] = {}
    texted = {t["phone"] for t in directory.get("texted", [])}
    for contact in directory.get("contacts", []):
        phones = [p for p in contact.get("phones", []) if p not in rows]
        if not phones or any(p in known for p in contact.get("phones", [])):
            continue  # one customer per contact, and none if any of its numbers is already a customer
        first, last, company = split_name(contact)
        phone = next((p for p in phones if p in texted), phones[0])
        rows[phone] = {"phone": phone, "first": first, "last": last, "company": company,
                       "fleet": bool(company) and not (contact.get("firstName") or contact.get("lastName")),
                       "contact": True, "inbound": 0, "outbound": 0, "last_at": None}
    for t in directory.get("texted", []):
        row = rows.setdefault(t["phone"], {"phone": t["phone"], "first": "", "last": "", "company": None, "fleet": False,
                                           "contact": False, "inbound": 0, "outbound": 0, "last_at": None})
        row["inbound"], row["outbound"], row["last_at"] = t.get("inbound", 0), t.get("outbound", 0), t.get("lastMessageAt")
        row["in_quo"] = bool(t.get("inQuo"))
    out = []
    for row in rows.values():
        reason = "already a customer" if row["phone"] in known else skip_reason(row["phone"])
        if reason == "already a customer":
            continue
        row["skip"] = reason
        # Every real number is ticked: unnamed ones get their name looked up from their own texts and calls.
        row["suggested"] = reason is None
        out.append(row)
    out.sort(key=lambda r: (r["skip"] is not None, not r["suggested"], -(r["inbound"] + r["outbound"]),
                            str(r["last_at"] or "")), reverse=False)
    return out


def pretty(phone: str) -> str:
    return f"({phone[:3]}) {phone[3:6]}-{phone[6:]}" if len(phone) == 10 else phone


def create(db: Session, row: dict, how: str) -> Customer:
    first = row.get("first") or "Unknown"
    last = row.get("last") or ("" if row.get("first") else f"({row['phone'][-4:]})")
    customer = Customer(first_name=first[:64], last_name=last[:64] or "-", phone="+1" + row["phone"],
                        customer_type="fleet" if row.get("fleet") else "individual",
                        fleet_name=row.get("company") if row.get("fleet") else None,
                        notes=f"Added {how} on {datetime.now().strftime('%b %-d, %Y')}." + ("" if row.get("first") else " Imperial is looking for their name in their texts and calls."),
                        name_source=row.get("source") or ("quo_contact" if row.get("contact") and row.get("first") else ("quo" if row.get("first") else None)))
    db.add(customer)
    db.flush()
    # Earlier queue items from this number now belong to the customer file.
    for item in db.query(DispatchItem).filter(DispatchItem.customer_id.is_(None)).all():
        if digits(item.customer_phone) == row["phone"]:
            item.customer_id = customer.id
    return customer


def import_rows(db: Session, rows: list[dict], chosen: set[str], actor: str) -> int:
    known = {digits(c.phone) for c in db.query(Customer).all() if c.phone}
    made = 0
    for row in rows:
        if row["phone"] in chosen and row["skip"] is None and row["phone"] not in known:
            create(db, row, "from Quo")
            known.add(row["phone"])
            made += 1
    db.add(DispatchAudit(item_id=None, actor=actor[:64], action="quo_customer_import", detail=f"{made} customers"))
    db.commit()
    return made


def auto_create(db: Session, phone: str, contact_name: str | None) -> Customer | None:
    """A new number texted or called: start their customer file. Called inside intake's transaction."""
    d = digits(phone)
    if skip_reason(d):
        return None
    name = (contact_name or "").strip()
    first, _, last = name.partition(" ")
    return create(db, {"phone": d, "first": first, "last": last, "company": None}, "automatically from Quo")
