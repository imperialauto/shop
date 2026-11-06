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
    "missed_call": "Missed calls and voicemails",
    "customer_reply": "Customer messages no rule can answer",
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
ARRIVAL = re.compile(r"\b(i'?m here|i am here|we'?re here|we are here|just (got here|arrived|pulled up)|pulled up|i'?m outside|anyone there|(at|by) (your|the) (front )?(door|shop|gate)|nobody(?:'?s| is) here|no one(?:'?s| is) here|are you (there|here|coming))\b", re.I)
WAITING = re.compile(r"^\W*(\?+|hello\W*|hey\W*|hi\W*)$", re.I)
# Phone tapbacks arrive as text, such as: 👍 to “See you then!”
REACTION = re.compile(r"^[\s\u200b]*(?:reacted\s+)?[^\w\s]{1,8}[\s\u200b]*to[\s\u200b]+[“\"]|^[\s\u200b]*(?:liked|loved|disliked|laughed at|emphasized|questioned)\s+[“\"]", re.I)
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


def is_reaction(body: str) -> bool:
    return bool(REACTION.match(body or ""))


def assess(body: str) -> Assessment:
    text = (body or "").strip()
    safety = bool(SAFETY.search(text))
    arrival = bool(ARRIVAL.search(text) or (WAITING.match(text) and "?" in text))
    urgency = "safety" if safety else "urgent" if arrival or URGENT.search(text) else "normal"

    if arrival and not safety:
        category = "arrival"
    elif INFO.search(text) and not (TOW.search(text) or PRICE.search(text) or safety):
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
    # Any price question goes to the owner, whatever else the message is about.
    if PRICE.search(text) and "unverified_estimate" not in reasons:
        reasons.append("unverified_estimate")

    match = VEHICLE.search(text)
    vehicle_text = " ".join(match.group(0).split()) if match else None
    vin_match = VIN.search(text.upper())
    missing = []
    if not vehicle_text and not vin_match and category != "arrival":
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
        "arrival": "The customer is here or waiting. Respond now.",
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


OWNER_EDIT = re.compile(r"^\s*edit\s*#?\s*(\d+)\s+(.+)$", re.I | re.S)
# Firm words may stand alone when one approval is open. Casual words also end
# ordinary conversation ("ok thanks"), so they only act alongside an item number.
APPROVE_WORDS = r"yes(?: please)?|y|approve|approved|send(?: it)?|go ahead|do it"
DECLINE_WORDS = r"no|n|decline|declined|skip(?: it)?|don'?t send(?: it)?|do not send(?: it)?"
CASUAL_APPROVE = r"ok|okay|sure|yep|yeah|looks good"
CASUAL_DECLINE = r"nope|cancel"
OWNER_COMMAND = re.compile(rf"^\s*({APPROVE_WORDS}|{DECLINE_WORDS})\s*#?\s*(\d+)?\s*[.!]?\s*$", re.I)
CASUAL_COMMAND = re.compile(rf"^\s*({CASUAL_APPROVE}|{CASUAL_DECLINE})\s*#?\s*(\d+)\s*[.!]?\s*$", re.I)


OWNER_PAUSE = re.compile(r"^\s*(?:stop|pause)\s+(?:all|everything)\s*[.!]?\s*$", re.I)
OWNER_RESUME = re.compile(r"^\s*(?:resume(?:\s+(?:all|everything))?|(?:re)?start\s+(?:all|everything))\s*[.!]?\s*$", re.I)
OWNER_DONE = re.compile(r"^\s*(?:done|handled|closed?)\s+((?:#?\d+[\s,]*)+)[.!]?\s*$", re.I)
OWNER_LATER = re.compile(r"^\s*(?:later|snooze)\s*#?\s*(\d+)(?:\s+(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minutes)?)?\s*$", re.I)
PROMISE = re.compile(r"\b(get back to you|follow up|i'?ll (check|look|find out|let you know|confirm|call|text)|call you back|shortly)\b", re.I)


def parse_owner_command(body: str):
    """("approve"|"decline", id or None), ("list", None), ("older", None), ("edit", id, text),
    ("done", [ids]), ("later", id, minutes), ("pause", None), ("resume", None), or None."""
    text = (body or "").strip()
    if OWNER_PAUSE.match(text):
        return "pause", None
    if OWNER_RESUME.match(text):
        return "resume", None
    finished = OWNER_DONE.match(text)
    if finished:
        return "done", [int(n) for n in re.findall(r"\d+", finished.group(1))]
    later = OWNER_LATER.match(text)
    if later:
        amount = int(later.group(2) or 2)
        minutes = amount if (later.group(3) or "h").lower().startswith("m") else amount * 60
        return "later", int(later.group(1)), min(minutes, 7 * 24 * 60)
    if re.fullmatch(r"old(er)?\??", text, re.I):
        return "older", None
    edited = OWNER_EDIT.match(text)
    if edited:
        return "edit", int(edited.group(1)), edited.group(2).strip()
    if re.fullmatch(r"(open|list|queue)\??", text, re.I):
        return "list", None
    match = OWNER_COMMAND.match(text) or CASUAL_COMMAND.match(text)
    if not match:
        return None
    declined = re.fullmatch(rf"{DECLINE_WORDS}|{CASUAL_DECLINE}", match.group(1), re.I)
    decision = "decline" if declined else "approve"
    return decision, int(match.group(2)) if match.group(2) else None


def apply_policies(reasons, modes: dict[str, str]) -> list[str]:
    """Reasons still needing the owner after their policy choices."""
    return [r for r in reasons if r in LOCKED_POLICIES or modes.get(r, "approve") == "approve"]
