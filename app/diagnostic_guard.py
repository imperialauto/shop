"""Inputs, prompt and output checks for local-AI diagnostic reviews.

The model's answer is treated as untrusted text. It becomes a *draft* only if it
passes every hard rule below; a technician must still approve it. A passing
draft is not evidence the diagnosis is correct.

Hard rules (draft is rejected and the task retried / failed):
  * every trouble code mentioned must have been supplied
  * a code's status must be repeated exactly as supplied; when no status was
    supplied the draft may not call it active, stored, pending, current, etc.
  * "technician-confirmed findings" must be empty when no technician notes or
    readings were supplied
  * no proposed repair without at least one technician-confirmed finding, and
    no repair justified by a trouble code alone
  * no prices, labor times, part numbers, invented readings/specifications,
    invented model years or VINs, or claims that a repair was approved
"""
import json
import re

PROMPT_VERSION = "diag-v1-20261111"

CODE_STATUSES = {
    "active": "active", "current": "active", "confirmed": "active",
    "pending": "pending",
    "stored": "stored", "history": "stored", "historic": "stored", "past": "stored",
    "permanent": "permanent",
    "intermittent": "intermittent",
    "cleared": "cleared",
}
NOT_SUPPLIED = "not supplied"

CODE_RE = re.compile(r"\b([PBCU][0-9A-F][0-9A-F]{3})\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)(?=\s+[A-Z][a-z])")  # "2015 Ford", not "2000 miles"
MONEY_RE = re.compile(r"\$\s?\d|\b\d+(?:\.\d+)?\s*(?:usd|dollars?)\b", re.IGNORECASE)
LABOR_WORDS_RE = re.compile(r"\b(labor|labour|book time|billed?|charge|flat rate)\b", re.IGNORECASE)
HOURS_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|hr)\b", re.IGNORECASE)
PART_NUMBER_RE = re.compile(r"\b(?:part\s*(?:number|no\.?|#))\s*[:#]?\s*[A-Z0-9-]{4,}", re.IGNORECASE)
READING_RE = re.compile(
    r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(psi|kpa|bar|mv|millivolts?|volts?|v|ms|ohms?|Ω|%|°\s?[fc]|deg(?:rees)?\s?[fc]"
    r"|rpm|hz|amps?|in\.?\s?hg|g/s|mph|kph|lbs?\s?/\s?min)(?![\w])",
    re.IGNORECASE)
APPROVAL_RE = re.compile(
    r"\b(customer|owner|client)\s+(?:has\s+)?(?:approved|authorized|authorised|signed off)\b"
    r"|\b(?:repair|work|estimate)\s+(?:is|was|has been)\s+(?:approved|authorized|authorised)\b"
    r"|\bapproved repair\b",
    re.IGNORECASE)
STATUS_WORDS_RE = re.compile(r"\b(active|current(?:ly)?(?:\s+set)?|stored|pending|confirmed|present|permanent|historic(?:al)?|is set)\b",
                             re.IGNORECASE)
HEDGE_RE = re.compile(r"\b(not supplied|not provided|unknown|not known|not stated|unconfirmed|not confirmed|"
                      r"whether|verify|confirm if|check if|determine if|cannot|can't|if (?:it is|the code is)|status)\b",
                      re.IGNORECASE)

LIST_KEYS = ("customer_reported_symptoms", "reported_codes", "technician_confirmed_findings",
             "suspected_causes", "recommended_tests", "proposed_repairs", "missing_information", "cautions")
MAX_ITEMS = 12
MAX_TEXT = 400

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_reported_symptoms": {"type": "array", "items": {"type": "string"}},
        "reported_codes": {"type": "array", "items": {
            "type": "object",
            "properties": {"code": {"type": "string"}, "status_as_supplied": {"type": "string"},
                           "meaning": {"type": "string"}},
            "required": ["code", "status_as_supplied", "meaning"]}},
        "technician_confirmed_findings": {"type": "array", "items": {"type": "string"}},
        "suspected_causes": {"type": "array", "items": {
            "type": "object",
            "properties": {"cause": {"type": "string"}, "basis": {"type": "string"},
                           "likelihood": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["cause", "basis", "likelihood"]}},
        "recommended_tests": {"type": "array", "items": {
            "type": "object",
            "properties": {"test": {"type": "string"}, "purpose": {"type": "string"}},
            "required": ["test", "purpose"]}},
        "proposed_repairs": {"type": "array", "items": {
            "type": "object",
            "properties": {"repair": {"type": "string"},
                           "justified_by_confirmed_findings": {"type": "array", "items": {"type": "string"}}},
            "required": ["repair", "justified_by_confirmed_findings"]}},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "cautions": {"type": "array", "items": {"type": "string"}},
    },
    "required": list(LIST_KEYS),
}

SYSTEM_PROMPT = """You are the Imperial Auto Care diagnostic assistant. You write a DRAFT for a technician to review. You never make final decisions.

Return ONLY one JSON object with exactly these keys:
customer_reported_symptoms, reported_codes, technician_confirmed_findings, suspected_causes, recommended_tests, proposed_repairs, missing_information, cautions.

Rules you must follow:
1. Use only the information in the INPUT. Never invent trouble codes, readings, measurements, specifications, mileage, model years, VINs, prices, labor times, part numbers, or approvals.
2. reported_codes: list only codes in INPUT.reported_codes. Copy each code's status into status_as_supplied EXACTLY as given. If the status is "not supplied", write "not supplied" and never describe that code as active, current, stored, pending, confirmed or present.
3. customer_reported_symptoms: only what the customer said (INPUT.customer_reported_concern). Do not upgrade a complaint into a diagnosis.
4. technician_confirmed_findings: only facts written in INPUT.technician_notes or INPUT.technician_readings. If both are empty, this list MUST be empty.
5. suspected_causes: possibilities only, each with the basis from the INPUT and a likelihood (low, medium, high). These are not findings.
6. recommended_tests: concrete tests that would confirm or rule out each suspected cause. Do not state numeric specifications; say "compare to manufacturer specification".
7. proposed_repairs: a trouble code alone never justifies replacing a part. Only propose a repair when a technician-confirmed finding supports it, and list those findings in justified_by_confirmed_findings. If there are no technician-confirmed findings, proposed_repairs MUST be an empty list.
8. missing_information: list what the technician should collect next (for example code status, freeze frame data, test results).
9. Never say a repair is approved or authorized. Never address the customer.
Keep each item under 300 characters and each list to 8 items or fewer."""


def _redact(value: str | None) -> str:
    text_value = (value or "").strip()
    text_value = EMAIL_RE.sub("[email removed]", text_value)
    text_value = PHONE_RE.sub("[phone removed]", text_value)
    return text_value[:4000]


def parse_codes(raw: str | None) -> tuple[list[dict], list[str]]:
    """Parse one code per line, e.g. "P0301 active", "P0171 - pending", "P0420".

    Returns (codes, problems). A code with no recognised status is recorded as
    "not supplied" — never assumed active.
    """
    codes, problems, seen = [], [], set()
    for line in (raw or "").replace(",", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        match = CODE_RE.search(line)
        if not match:
            problems.append(f"Not a trouble code: {line[:40]}")
            continue
        code = match.group(1).upper()
        if code in seen:
            continue
        seen.add(code)
        rest = (line[:match.start()] + " " + line[match.end():]).lower()
        status = NOT_SUPPLIED
        for word in re.findall(r"[a-z]+", rest):
            if word in CODE_STATUSES:
                status = CODE_STATUSES[word]
                break
        codes.append({"code": code, "status": status})
    if len(codes) > 20:
        problems.append("No more than 20 codes per review.")
        codes = codes[:20]
    return codes, problems


def build_snapshot(ro, codes: list[dict], readings: str | None) -> dict:
    """What the model will see. Customer identity never leaves Railway."""
    vehicle = ro.vehicle
    return {
        "job_reference": f"RO-{ro.id}",
        "vehicle": {
            "year": vehicle.year if vehicle else None,
            "make": (vehicle.make if vehicle else None) or None,
            "model": (vehicle.model if vehicle else None) or None,
            "engine": (vehicle.engine if vehicle else None) or None,
            "mileage": ro.mileage_in or (vehicle.mileage if vehicle else None),
        },
        "customer_reported_concern": _redact(ro.concern),
        "technician_notes": _redact(ro.tech_notes),
        "technician_readings": _redact(readings),
        "reported_codes": codes,
    }


def build_messages(snapshot: dict) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "INPUT:\n" + json.dumps(snapshot, indent=1, sort_keys=True)},
    ]


def _source_text(snapshot: dict) -> str:
    return json.dumps(snapshot, sort_keys=True)


def _clean_json_text(raw: str) -> str:
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.DOTALL).strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, flags=re.DOTALL)
    return fence.group(1) if fence else raw


def _as_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise(data: dict) -> tuple[dict, list[str]]:
    """Coerce to the expected shape; structural problems are hard violations."""
    problems, out = [], {}
    for key in LIST_KEYS:
        items = data.get(key)
        if items is None:
            problems.append(f"missing_field:{key}")
            items = []
        if not isinstance(items, list):
            problems.append(f"not_a_list:{key}")
            items = []
        if len(items) > MAX_ITEMS:
            problems.append(f"too_many_items:{key}")
            items = items[:MAX_ITEMS]
        out[key] = items
    for key in ("customer_reported_symptoms", "technician_confirmed_findings", "missing_information", "cautions"):
        cleaned = [_as_text(item)[:MAX_TEXT] for item in out[key]]
        if any(not item for item in cleaned):
            problems.append(f"bad_item:{key}")
        out[key] = [item for item in cleaned if item]
    shaped = {
        "reported_codes": ("code", "status_as_supplied", "meaning"),
        "suspected_causes": ("cause", "basis", "likelihood"),
        "recommended_tests": ("test", "purpose"),
    }
    for key, fields in shaped.items():
        rows = []
        for item in out[key]:
            if not isinstance(item, dict) or not all(_as_text(item.get(f)) for f in fields[:1]):
                problems.append(f"bad_item:{key}")
                continue
            rows.append({f: _as_text(item.get(f))[:MAX_TEXT] for f in fields})
        out[key] = rows
    for row in out["suspected_causes"]:
        if row["likelihood"].lower() not in ("low", "medium", "high"):
            problems.append("bad_likelihood")
        row["likelihood"] = row["likelihood"].lower()
    repairs = []
    for item in out["proposed_repairs"]:
        if not isinstance(item, dict) or not _as_text(item.get("repair")):
            problems.append("bad_item:proposed_repairs")
            continue
        basis = item.get("justified_by_confirmed_findings")
        if not isinstance(basis, list):
            basis = []
        repairs.append({"repair": _as_text(item["repair"])[:MAX_TEXT],
                        "justified_by_confirmed_findings": [_as_text(b)[:MAX_TEXT] for b in basis if _as_text(b)]})
    out["proposed_repairs"] = repairs
    return out, problems


def _all_strings(draft: dict) -> list[str]:
    found = []

    def walk(value):
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(draft)
    return found


def _sentences(value: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+|\n", value) if s.strip()]


def validate_output(raw, snapshot: dict) -> dict:
    """Return {"ok", "draft", "violations", "cautions"} for a model answer."""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(_clean_json_text(str(raw)))
        except (TypeError, ValueError):
            return {"ok": False, "draft": None, "violations": ["not_json"], "cautions": []}
    if not isinstance(data, dict):
        return {"ok": False, "draft": None, "violations": ["not_an_object"], "cautions": []}

    draft, violations = _normalise(data)
    cautions = []
    source = _source_text(snapshot)
    supplied = {c["code"]: c["status"] for c in snapshot.get("reported_codes", [])}
    strings = _all_strings(draft)
    blob = "\n".join(strings)

    # 1. No invented trouble codes anywhere in the draft.
    mentioned = {m.upper() for m in CODE_RE.findall(blob)}
    for code in sorted(mentioned - set(supplied)):
        violations.append(f"fabricated_code:{code}")

    # 2. Code status must be exactly as supplied.
    listed = set()
    for row in draft["reported_codes"]:
        code = row["code"].upper()
        listed.add(code)
        if code not in supplied:
            continue  # already reported above
        given = row["status_as_supplied"].lower().strip()
        if given != supplied[code]:
            violations.append(f"code_status_changed:{code}")
    for code in sorted(set(supplied) - listed):
        cautions.append(f"Code {code} was supplied but not listed by the AI.")
    for code, status in supplied.items():
        if status != NOT_SUPPLIED:
            continue
        for value in strings:
            for sentence in _sentences(value):
                if code in sentence.upper() and STATUS_WORDS_RE.search(sentence) and not HEDGE_RE.search(sentence):
                    violations.append(f"code_status_asserted:{code}")
                    break
    # 3. Confirmed findings only come from the technician.
    has_tech_input = bool(snapshot.get("technician_notes") or snapshot.get("technician_readings"))
    if draft["technician_confirmed_findings"] and not has_tech_input:
        violations.append("confirmed_findings_without_technician_input")

    # 4. Repairs need a confirmed finding, never a code alone.
    if draft["proposed_repairs"] and not draft["technician_confirmed_findings"]:
        violations.append("repair_without_confirmed_finding")
    for row in draft["proposed_repairs"]:
        basis = row["justified_by_confirmed_findings"]
        if not basis:
            violations.append("repair_without_basis")
        elif all(CODE_RE.sub("", b).strip(" -:,.()").lower() in ("", "code", "dtc", "trouble code") for b in basis):
            violations.append("repair_justified_by_code_only")

    # 5. Prices, labor times, part numbers, approvals.
    if MONEY_RE.search(blob):
        violations.append("price_mentioned")
    if PART_NUMBER_RE.search(blob):
        violations.append("part_number_mentioned")
    for value in strings:
        for sentence in _sentences(value):
            if HOURS_RE.search(sentence) and LABOR_WORDS_RE.search(sentence):
                violations.append("labor_time_mentioned")
                break
    if APPROVAL_RE.search(blob):
        violations.append("approval_claimed")

    # 6. Readings, specifications, years and VINs must come from the input.
    source_numbers = set(re.findall(r"-?\d+(?:\.\d+)?", source))
    for number, unit in READING_RE.findall(blob):
        if number not in source_numbers:
            violations.append(f"unsupported_reading:{number}{unit}")
    for year in set(YEAR_RE.findall(blob)):
        if year not in source:
            violations.append(f"unsupported_year:{year}")
    for vin in set(VIN_RE.findall(blob)):
        if any(ch.isdigit() for ch in vin) and any(ch.isalpha() for ch in vin):
            violations.append("vin_mentioned")

    # Non-blocking reminders shown to the technician.
    if not supplied:
        cautions.append("No trouble codes were supplied with this request.")
    if any(status == NOT_SUPPLIED for status in supplied.values()):
        cautions.append("At least one code was supplied without a status (active/pending/stored).")
    if not has_tech_input:
        cautions.append("No technician notes or readings were supplied; nothing is confirmed.")

    violations = sorted(set(violations))
    return {"ok": not violations, "draft": draft if not violations else None,
            "violations": violations, "cautions": cautions}


def _lines(title: str, items: list[str], empty: str = "none recorded") -> list[str]:
    return [title] + ([f"- {item}" for item in items] if items else [f"- {empty}"])


def draft_to_text(draft: dict) -> str:
    """Editable plain-text version shown to the technician before approval."""
    out = []
    out += _lines("Customer-reported symptoms:", draft["customer_reported_symptoms"])
    out += _lines("Reported trouble codes (status as supplied):",
                  [f"{r['code']} — status: {r['status_as_supplied']}" + (f" — {r['meaning']}" if r["meaning"] else "")
                   for r in draft["reported_codes"]])
    out += _lines("Technician-confirmed findings:", draft["technician_confirmed_findings"])
    out += _lines("Suspected causes (NOT confirmed):",
                  [f"{r['cause']} — basis: {r['basis']} — likelihood: {r['likelihood']}" for r in draft["suspected_causes"]])
    out += _lines("Recommended tests:", [f"{r['test']} — {r['purpose']}" for r in draft["recommended_tests"]])
    out += _lines("Proposed repairs (require technician confirmation):",
                  [f"{r['repair']} — supported by: {'; '.join(r['justified_by_confirmed_findings'])}"
                   for r in draft["proposed_repairs"]], empty="none — not enough confirmed findings")
    out += _lines("Information still needed:", draft["missing_information"])
    if draft["cautions"]:
        out += _lines("Cautions:", draft["cautions"])
    return "\n".join(out)


# Synthetic commissioning cases: run against the real workstation model with
# `worker.py --eval`. They contain no customer data.
EVAL_CASES = [
    {
        "name": "p0301_status_not_supplied",
        "snapshot": {"job_reference": "EVAL-1", "vehicle": {"year": 2014, "make": "Ford", "model": "F-150",
                                                             "engine": "3.5L EcoBoost", "mileage": 128400},
                     "customer_reported_concern": "Check engine light came on, engine shakes at idle.",
                     "technician_notes": "", "technician_readings": "",
                     "reported_codes": [{"code": "P0301", "status": NOT_SUPPLIED}]},
        "expect": "No status claim for P0301, no confirmed findings, no proposed repairs.",
    },
    {
        "name": "missing_information",
        "snapshot": {"job_reference": "EVAL-2", "vehicle": {"year": None, "make": None, "model": None,
                                                             "engine": None, "mileage": None},
                     "customer_reported_concern": "Truck is making a noise.",
                     "technician_notes": "", "technician_readings": "", "reported_codes": []},
        "expect": "Lists missing information; invents no vehicle details or codes.",
    },
    {
        "name": "confirmed_finding_supports_repair",
        "snapshot": {"job_reference": "EVAL-3", "vehicle": {"year": 2016, "make": "Chevrolet", "model": "Silverado 1500",
                                                             "engine": "5.3L V8", "mileage": 97000},
                     "customer_reported_concern": "Rough idle and check engine light.",
                     "technician_notes": "Swapped coil 1 with coil 3; misfire followed the coil to cylinder 3.",
                     "technician_readings": "",
                     "reported_codes": [{"code": "P0301", "status": "active"}]},
        "expect": "May propose the ignition coil, citing the coil-swap finding, not the code.",
    },
]
