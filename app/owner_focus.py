"""Works out which repair order the owner's text is about, before any AI reads it.

"I believe it's eight for Antoine Chevy Impala SS" names RO #8 by number word, a
misspelt first name and the vehicle. Approval numbers (#111) and task numbers are
different things, so this only resolves repair orders and says plainly when it
can't tell. It never guesses between two close matches.
"""
import difflib
import re

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
MAKE_ALIASES = {"chevy": "chevrolet", "vw": "volkswagen", "benz": "mercedes-benz", "mercedes": "mercedes-benz",
                "dodge": "dodge", "ram": "ram", "ford": "ford"}
EXPLICIT_RO = re.compile(r"\b(?:ro|r\.o\.?|repair\s+order|job)\s*(?:number|no\.?|#)?\s*#?\s*(\d{1,5})\b", re.I)
WORDS = re.compile(r"[a-z0-9][a-z0-9'-]+", re.I)
APPROVAL_REF = re.compile(r"(?:^|\s)#\s*(\d{1,5})\b")
STOP = {"the", "and", "for", "with", "this", "that", "just", "about", "number", "there", "their", "they",
        "its", "it's", "not", "able", "our", "repair", "headlight", "working", "photos", "upload", "complete"}


def _close(a: str, b: str) -> bool:
    if not a or not b or len(a) < 3 or len(b) < 3:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _numbers(text: str) -> set[int]:
    found = {int(n) for n in re.findall(r"\b(\d{1,5})\b", text)}
    found |= {NUMBER_WORDS[w] for w in (w.lower() for w in WORDS.findall(text)) if w in NUMBER_WORDS}
    return found


def _score(ro, words: list[str], numbers: set[int]) -> int:
    score = 0
    customer, vehicle = ro.customer, ro.vehicle
    names = [(customer.first_name or "").lower(), (customer.last_name or "").lower()] if customer else []
    if any(_close(w, n) for w in words for n in names if n):
        score += 2
    if vehicle is not None:
        make = (vehicle.make or "").lower()
        model_words = [m for m in WORDS.findall((vehicle.model or "").lower()) if len(m) >= 3]
        if any(MAKE_ALIASES.get(w, w) == make for w in words if make):
            score += 1
        if any(_close(w, m) for w in words for m in model_words):
            score += 2
        if vehicle.year and vehicle.year in numbers:
            score += 1
    if ro.id in numbers:
        score += 2
    return score


def resolve(db, text: str, open_statuses_only: bool = True) -> dict:
    """{"ro": {...}} when one repair order clearly matches, {"candidates": [...]} when several do,
    {} when nothing in the text points at a repair order."""
    from app.models import RepairOrder

    text = text or ""
    explicit = EXPLICIT_RO.search(text)
    if explicit:
        ro = db.get(RepairOrder, int(explicit.group(1)))
        return {"ro": summary(ro), "how": "explicit"} if ro else {"missing": int(explicit.group(1))}
    words = [w.lower() for w in WORDS.findall(text) if w.lower() not in STOP]
    numbers = _numbers(text) - {int(n) for n in APPROVAL_REF.findall(text)}  # "#111" is an approval, not an RO
    query = db.query(RepairOrder)
    if open_statuses_only:
        query = query.filter(RepairOrder.status != "delivered")
    scored = sorted(((_score(ro, words, numbers), ro) for ro in query.order_by(RepairOrder.id.desc()).limit(200)),
                    key=lambda pair: -pair[0])
    scored = [(score, ro) for score, ro in scored if score >= 3]
    if not scored:
        return {}
    if len(scored) == 1 or scored[0][0] - scored[1][0] >= 2:
        return {"ro": summary(scored[0][1]), "how": "matched"}
    return {"candidates": [summary(ro) for _, ro in scored[:3]]}


def summary(ro) -> dict | None:
    if ro is None:
        return None
    customer, vehicle = ro.customer, ro.vehicle
    vehicle_text = " ".join(str(part) for part in (vehicle.year, vehicle.make, vehicle.model) if part) if vehicle else ""
    return {
        "roId": ro.id,
        "label": f"RO #{ro.id}",
        "customer": f"{customer.first_name} {customer.last_name}".strip() if customer else "",
        "firstName": (customer.first_name or "") if customer else "",
        "phone": (customer.phone or "") if customer else "",
        "vehicle": vehicle_text,
        "status": ro.status,
        "lines": [{
            "id": line.id, "description": (line.description or "")[:120], "approval": line.approval,
            "completed": bool(getattr(line, "completed_at", None)),
        } for line in ro.line_items],
    }
