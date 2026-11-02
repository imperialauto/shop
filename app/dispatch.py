"""Rules that sort an inbound customer message. No database or network access.

Deliberately rule-based: it must behave the same every time, and it errs toward
showing the owner. A negated phrase such as "no fuel leak" still flags safety.
"""
import re
from dataclasses import dataclass, field

SHOP_DETAILS = (
    "Imperial Auto Care is at 7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307. "
    "We're open Mon-Fri 11 AM-7 PM and Sat 12-5 PM, closed Sunday."
)
ASK_VEHICLE = "Thanks for reaching out to Imperial Auto Care! What's the year, make and model of your vehicle?"
# Automatic replies the owner approved. Modes are "auto" and "off".
AUTO_POLICIES = {
    "auto_hours_address": "Answer hours and address questions",
    "auto_vehicle_details": "Ask for missing year, make and model",
}

# Each policy maps to what the owner sees; "approve" is always the default.
POLICIES = {
    "discount_credit": "Nonstandard discounts and credits",
    "unverified_estimate": "Unverified repair estimates",
    "major_parts": "Major parts purchases",
    "refund": "Refunds",
    "unusual_towing": "Unusual towing commitments",
    "scheduling_conflict": "Scheduling conflicts",
    "additional_authorization": "Repairs needing more customer authorization",
    "high_risk": "High-risk customer or operational exceptions",
}
# The owner can relax the others to notify-only. These two never relax.
LOCKED_POLICIES = {"high_risk", "refund"}

SAFETY = re.compile(
    r"\b(fuel leak|leaking (gas|fuel)|smell(s|ing)? (of )?(gas|fuel)|on fire|fire|smoke|smoking|"
    r"overheat\w*|low oil pressure|oil pressure|flashing (check )?(engine|cel|light)|"
    r"(no|lost|losing) brakes?|brakes? (failed|gone|went out)|"
    r"(engine|car|truck) (stopped|died|shut off|quit)|stalled|stranded|accident|crash\w*)\b", re.I)
URGENT = re.compile(r"\b(asap|urgent\w*|emergency|right now|today|need (it|my car|my truck) (back|fixed)|won'?t start)\b", re.I)
TOW = re.compile(r"\b(tow\w*|flatbed|roadside)\b", re.I)
SCHEDULE = re.compile(r"\b(appointment|schedule\w*|reschedule\w*|book\w*|available|availability|what time|come in|drop (it )?off)\b", re.I)
PRICE = re.compile(r"\b(how much|price|pricing|quote|estimate|cost)\b", re.I)
INFO = re.compile(r"\b(your hours|what are (your|the) hours|hours\?|what time do you|when do you (open|close)|are you open|your address|what'?s the address|where are you|where'?s the shop|your location)\b", re.I)
STATUS = re.compile(r"\b(status|is it ready|ready yet|done yet|any update|update on)\b", re.I)
REASONS = {
    "discount_credit": re.compile(r"\b(discount|credit|coupon|cheaper|price match|waive)\b", re.I),
    "refund": re.compile(r"\b(refund|money back|chargeback|dispute)\b", re.I),
    "major_parts": re.compile(r"\b(new|rebuilt|reman\w*|used|replace\w*) (engine|transmission|turbo|injectors?)\b", re.I),
    "unusual_towing": re.compile(r"\b(tow\w*|flatbed)\b.*\b(free|cover|pay for|out of town|long distance|credit)\b", re.I),
    "scheduling_conflict": re.compile(r"\b(reschedule\w*|double.?booked|can'?t make it|conflict)\b", re.I),
    "additional_authorization": re.compile(r"\b(go ahead|approve|authorize|also fix|add(ing)? to)\b", re.I),
    "high_risk": re.compile(r"\b(lawyer|attorney|sue|small claims|complaint|bbb|police|insurance claim)\b", re.I),
}
MAKES = (
    "acura|audi|bmw|buick|cadillac|chevy|chevrolet|chrysler|dodge|fiat|ford|gmc|honda|hyundai|infiniti|"
    "jeep|kia|lexus|lincoln|mazda|mercedes|mini|mitsubishi|nissan|ram|subaru|tesla|toyota|volkswagen|vw|volvo"
)
VEHICLE = re.compile(rf"\b((?:19|20)\d{{2}})\s+({MAKES})\b((?:\s+[A-Za-z0-9-]+)?)", re.I)
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
ADDRESS_HINT = re.compile(r"\b(\d{2,6}\s+\w+|i-?\d{1,3}|highway|hwy|exit|located in|i'?m (in|at|near))\b", re.I)


@dataclass(frozen=True)
class Assessment:
    category: str
    urgency: str
    vehicle_text: str | None
    vin: str | None
    reasons: tuple[str, ...] = field(default_factory=tuple)
    missing: tuple[str, ...] = field(default_factory=tuple)
    proposed_action: str = ""


def assess(body: str) -> Assessment:
    text = (body or "").strip()
    safety = bool(SAFETY.search(text))
    urgency = "safety" if safety else "urgent" if URGENT.search(text) else "normal"

    if INFO.search(text) and not (TOW.search(text) or PRICE.search(text) or safety):
        category = "info"
    elif TOW.search(text):
        category = "towing"
    elif STATUS.search(text):
        category = "status"
    elif SCHEDULE.search(text):
        category = "scheduling"
    elif PRICE.search(text):
        category = "pricing"
    elif safety or re.search(r"\b(noise|leak\w*|check engine|shak\w*|won'?t|broke\w*|problem|issue)\b", text, re.I):
        category = "repair"
    else:
        category = "general"

    reasons = [key for key, pattern in REASONS.items() if pattern.search(text)]
    if safety and "high_risk" not in reasons:
        reasons.append("high_risk")
    if category == "pricing" and "unverified_estimate" not in reasons:
        reasons.append("unverified_estimate")

    match = VEHICLE.search(text)
    vehicle_text = " ".join(match.group(0).split()) if match else None
    vin_match = VIN.search(text.upper())
    missing = []
    if not vehicle_text and not vin_match:
        missing.append("year, make and model")
    if category in ("towing", "repair") and not ADDRESS_HINT.search(text):
        missing.append("vehicle location")
    return Assessment(category, urgency, vehicle_text, vin_match.group(0) if vin_match else None,
                      tuple(reasons), tuple(missing), propose(category, urgency, missing))


def propose(category: str, urgency: str, missing: list[str]) -> str:
    steps = []
    if urgency == "safety":
        steps.append("Call or reply first: possible safety issue, advise before any driving.")
    steps.append({
        "info": "Share the shop hours and address.",
        "towing": "Confirm the vehicle rolls and steers, then the tow timing.",
        "status": "Check the job status, then send an approved update.",
        "scheduling": "Check confirmed availability before offering a time.",
        "pricing": "Verify parts and labor before quoting anything beyond standard diagnostic pricing.",
        "repair": "Offer the standard diagnostic and find out shop or mobile.",
        "general": "Review the message and reply.",
    }[category])
    if missing:
        steps.append("Ask for: " + ", ".join(missing) + ".")
    return " ".join(steps)


def auto_reply(result: Assessment, has_vehicle: bool, modes: dict[str, str]):
    """(template, text) only for a plain routine message, otherwise None."""
    if result.urgency != "normal" or result.reasons:
        return None
    if result.category == "info" and modes.get("auto_hours_address", "auto") == "auto":
        return "hours", SHOP_DETAILS
    if (result.category in ("repair", "scheduling") and not has_vehicle
            and "year, make and model" in result.missing
            and modes.get("auto_vehicle_details", "auto") == "auto"):
        return "vehicle", ASK_VEHICLE
    return None


OWNER_COMMAND = re.compile(r"^\s*(yes|y|approve|approved|no|n|decline)\s*#?\s*(\d+)?\s*[.!]?\s*$", re.I)


def parse_owner_command(body: str):
    """("approve"|"decline", item id or None), ("list", None), or None."""
    text = (body or "").strip()
    if re.fullmatch(r"(open|list|queue)\??", text, re.I):
        return "list", None
    match = OWNER_COMMAND.match(text)
    if not match:
        return None
    decision = "approve" if match.group(1).lower().startswith(("y", "a")) else "decline"
    return decision, int(match.group(2)) if match.group(2) else None


def apply_policies(reasons, modes: dict[str, str]) -> list[str]:
    """Reasons still needing the owner after their policy choices."""
    return [r for r in reasons if r in LOCKED_POLICIES or modes.get(r, "approve") == "approve"]
