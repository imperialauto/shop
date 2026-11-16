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
# "RO 8", "approval 12", "task #118", "fix list 23": the word before a number says what kind of
# record it is. Only RO-typed (or untyped) numbers may point at a repair order.
TYPES = {
    "ro": "ro", "r.o": "ro", "r.o.": "ro", "repair order": "ro", "job": "ro", "work order": "ro",
    "approval": "approval", "approvals": "approval", "item": "approval", "queue item": "approval",
    "line": "line", "line item": "line", "lines": "line",
    "task": "task", "tasks": "task", "to-do": "task", "todo": "task",
    "fix": "fix", "fix list": "fix", "report": "fix", "problem": "fix",
    "invoice": "invoice", "estimate": "estimate", "appointment": "appointment", "appt": "appointment",
}
TYPED_REF = re.compile(
    r"\b(repair\s+order|work\s+order|queue\s+item|line\s+item|fix\s+list|r\.o\.?|ro|job|approvals?|lines?|item|tasks?|to-?do|fix|"
    r"report|problem|invoice|estimate|appointment|appt)\s*(?:number|no\.?|num|#)?\s*#?\s*(\d{1,5}|"
    + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")\b", re.I)
EXPLICIT_RO = re.compile(r"\b(?:ro|r\.o\.?|repair\s+order|work\s+order|job)\s*(?:number|no\.?|#)?\s*#?\s*(\d{1,5})\b", re.I)
WORDS = re.compile(r"[a-z0-9][a-z0-9'-]+", re.I)
APPROVAL_REF = re.compile(r"(?:^|\s)#\s*(\d{1,5})\b")
STOP = {"the", "and", "for", "with", "this", "that", "just", "about", "number", "there", "their", "they",
        "its", "it's", "not", "able", "our", "repair", "headlight", "working", "photos", "upload", "complete"}


def _close(a: str, b: str) -> bool:
    if not a or not b or len(a) < 3 or len(b) < 3:
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def typed_refs(text: str) -> list[dict]:
    """Every "<record type> <number>" in the text, in order, with its normalized type."""
    out = []
    for match in TYPED_REF.finditer(text or ""):
        word = " ".join(match.group(1).lower().split()).rstrip(".")
        kind = TYPES.get(word) or TYPES.get(word + ".") or ("ro" if word.startswith("r.o") else None)
        if kind:
            raw = match.group(2).lower()
            ref = {"type": kind, "id": int(raw) if raw.isdigit() else NUMBER_WORDS[raw]}
            if ref not in out:
                out.append(ref)
    return out


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
    refs = typed_refs(text)
    ro_refs = [r["id"] for r in refs if r["type"] == "ro"]
    other = [r for r in refs if r["type"] != "ro"]

    def extra(ro=None):
        if not refs:
            return {}
        return {"refs": refs, "records": [record(db, r["type"], r["id"], ro) for r in other]}

    distinct = list(dict.fromkeys(ro_refs))  # "RO 8 ... RO #8" is one job
    if len(distinct) > 1:
        # "RO 8 or RO 12": two different jobs named. Never pick one; ask which.
        found = [ro for ro in (db.get(RepairOrder, n) for n in distinct) if ro is not None]
        missing = [n for n in distinct if n not in {ro.id for ro in found}]
        out = {"candidates": [summary(ro) for ro in found], "how": "named_several", **extra()}
        if missing:
            out["missingRos"] = missing
        return out
    if distinct:
        ro = db.get(RepairOrder, distinct[0])
        if ro is None:
            return {"missing": distinct[0], "missingType": "ro", **extra()}
        return {"ro": summary(ro), "how": "explicit", **extra(ro)}
    words = [w.lower() for w in WORDS.findall(text) if w.lower() not in STOP and w.lower() not in TYPES]
    # A number typed as another record ("approval 12", "task #118") or written "#111" is never an RO number.
    numbers = _numbers(text) - {int(n) for n in APPROVAL_REF.findall(text)} - {r["id"] for r in other}
    query = db.query(RepairOrder)
    if open_statuses_only:
        query = query.filter(RepairOrder.status != "delivered")
    scored = sorted(((_score(ro, words, numbers), ro) for ro in query.order_by(RepairOrder.id.desc()).limit(200)),
                    key=lambda pair: -pair[0])
    scored = [(score, ro) for score, ro in scored if score >= 3]
    if not scored:
        return extra()
    if len(scored) == 1 or scored[0][0] - scored[1][0] >= 2:
        return {"ro": summary(scored[0][1]), "how": "matched", **extra(scored[0][1])}
    return {"candidates": [summary(ro) for _, ro in scored[:3]], **extra()}


def record(db, kind: str, record_id: int, focus_ro=None) -> dict:
    """Look up a non-RO record the owner named, so a reply can say exactly what it is, that it
    doesn't exist, or that it belongs to a different job than the one he's talking about.
    Read-only. "exists": False only when the lookup ran and found nothing; a failed lookup is
    "lookupFailed", and a kind with no numbered record (estimates live on the RO) is
    "resolvable": False, so neither is ever reported as missing."""
    found = {"type": kind, "id": record_id, "exists": False, "resolvable": True}
    ro_id = None
    try:
        if kind == "approval":
            from app.dispatch_model import DispatchItem
            item = db.get(DispatchItem, record_id)
            if item is not None:
                link = linked_ro(db, item)
                ro_id = link.get("roId")
                found.update(exists=True, status=item.status, label=f"approval #{record_id}",
                             who=item.contact_name or item.customer_phone or "", customerId=item.customer_id,
                             roLink=link["how"], **({"roId": ro_id} if ro_id else {}))
        elif kind in ("task", "fix"):
            from app.imperial_task_model import PROBLEM_CATEGORY, ImperialTask
            task = db.get(ImperialTask, record_id)
            if task is not None:
                is_fix = (task.category or "") == PROBLEM_CATEGORY
                actual = "fix" if is_fix else "task"
                found.update(exists=True, status=task.status, label=f"{'Fix list' if is_fix else 'task'} #{record_id}",
                             title=(task.title or "")[:120], actualType=actual, typeMismatch=actual != kind)
        elif kind == "invoice":
            from app.models import Invoice
            invoice = db.get(Invoice, record_id)
            if invoice is not None:
                ro_id = invoice.repair_order_id
                found.update(exists=True, status=invoice.status, label=f"invoice #{record_id}", roId=ro_id)
        elif kind == "appointment":
            from app.appointment_model import Appointment
            appointment = db.get(Appointment, record_id)
            if appointment is not None:
                ro_id = appointment.repair_order_id
                found.update(exists=True, status=appointment.status, label=f"appointment #{record_id}", roId=ro_id,
                             startsAt=appointment.starts_at.isoformat() if appointment.starts_at else None)
        elif kind == "line":
            from app.models import LineItem
            line = db.get(LineItem, record_id)
            if line is not None:
                ro_id = line.repair_order_id
                found.update(exists=True, label=f"line {record_id} on RO #{ro_id}", roId=ro_id,
                             description=(line.description or "")[:120], approval=line.approval,
                             completed=bool(getattr(line, "completed_at", None)))
        else:  # estimates aren't stored under their own number; they belong to an RO
            found.update(resolvable=False, label=f"{kind} {record_id}")
    except Exception:
        db.rollback()
        found.update(lookupFailed=True)
        return found
    if found["exists"] and focus_ro is not None:
        if ro_id is not None:
            found["matchesFocus"] = ro_id == focus_ro.id
        elif kind == "approval":
            # Same customer is not the same job (one customer, two vehicles). Without a
            # verified link to an RO, say so instead of attaching it to the job in focus.
            found["matchesFocus"] = None
            found["jobUnverified"] = True
    return found


def linked_ro(db, item) -> dict:
    """Which repair order an approval belongs to, only from a recorded link:
    1. the service lead that holds this queue item and an RO, or
    2. the item's own vehicle, when that vehicle has exactly one open RO.
    Customer alone never counts: one customer can have several jobs open."""
    import os
    from app.models import RepairOrder

    lead = None
    if os.getenv("SERVICE_LEADS_ENABLED", "").strip().lower() == "true":  # lead tables exist only when enabled
        from app.lead_model import ServiceLead
        lead = (db.query(ServiceLead).filter(ServiceLead.last_dispatch_item_id == item.id,
                                             ServiceLead.repair_order_id.isnot(None))
                .order_by(ServiceLead.id.desc()).first())
    if lead is not None:
        return {"roId": lead.repair_order_id, "how": "lead"}
    if item.vehicle_id:
        open_ros = db.query(RepairOrder).filter(RepairOrder.vehicle_id == item.vehicle_id,
                                                RepairOrder.status != "delivered").all()
        if len(open_ros) == 1:
            return {"roId": open_ros[0].id, "how": "vehicle"}
        return {"how": "vehicle_ambiguous" if open_ros else "vehicle_no_open_ro"}
    return {"how": "unverified"}


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
