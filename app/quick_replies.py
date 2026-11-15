"""One-tap reply suggestions for a dashboard item.

Each suggestion asks only for what the shop does not have yet, so the owner
never sends "what's the year, make and model?" to someone who already said
it, and never asks for a VIN twice. Nothing here sends anything: a tap only
fills the reply box for the owner to edit and send.
"""
import re

SHOP_ADDRESS = "7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307"
_VIN_ASKED = re.compile(r"\bvin\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")


def _known_name(customer, item) -> bool:
    if customer and (customer.first_name or "").strip() and (customer.last_name or "").strip():
        return True
    name = (item.contact_name or "").strip()
    return len(name.split()) >= 2 and not name.lower().startswith("unknown")


def _known_vehicle(customer, item) -> bool:
    if customer is not None and getattr(customer, "vehicles", None):
        return True
    return bool(_YEAR.search(item.vehicle_text or "")) and len((item.vehicle_text or "").split()) >= 3


def _has_vin(customer) -> bool:
    return any((v.vin or "").strip() for v in (getattr(customer, "vehicles", None) or []))


def _vin_asked(history, item) -> bool:
    texts = [getattr(entry, "body", "") or "" for entry in history or []]
    texts.append(item.draft_reply or "")
    return any(_VIN_ASKED.search(text) for text in texts)


def suggestions(item, customer=None, history=None) -> list[dict]:
    """Up to four short, editable replies for this conversation."""
    name, vehicle = _known_name(customer, item), _known_vehicle(customer, item)
    first = ""
    if customer and (customer.first_name or "").strip():
        first = customer.first_name.strip().split()[0]
    elif name:
        first = item.contact_name.strip().split()[0]
    hello = f"Hi {first}, " if first else "Hi, "
    out = []
    if not name and not vehicle:
        out.append({"label": "Ask name + vehicle", "text": (
            f"{hello}thanks for reaching out to Imperial Auto Care. Can I get your first and last name, "
            "and the year, make and model of the vehicle?")})
    elif not name:
        out.append({"label": "Ask name", "text": f"{hello}thanks for reaching out. Can I get your first and last name for the file?"})
    elif not vehicle:
        out.append({"label": "Ask vehicle", "text": f"{hello}what's the year, make and model?"})
    elif not _has_vin(customer) and not _vin_asked(history, item):
        out.append({"label": "Ask VIN (once)", "text": (
            f"{hello}if you have it handy, can you send the VIN? It's on the driver's side windshield "
            "or door jamb. No worries if not.")})
    out.append({"label": "Pick a time", "text": f"{hello}what day and time works best for you to bring it in?"})
    out.append({"label": "Shop address", "text": f"{hello}we're at {SHOP_ADDRESS}. Let me know when you're on the way."})
    out.append({"label": "Ask for photos", "text": f"{hello}can you send a couple photos of the issue? That helps me get you an accurate estimate."})
    return out[:4]
