"""Ask Imperial: three staff actions from one box.

1. Find a job and explain the next step  (read-only, answered right away)
2. Add an internal note or update a task (preview first; Apply writes, audited)
3. Prepare a labor-backed estimate draft (Local Node reads the request; an advisor verifies
   labor in ALLDATA and parts at AutoZonePro/O'Reilly; Imperial's rates and tax price it;
   Apply adds *draft* lines to the RO, never sent or approved)

Customer messages, appointment confirmation, repair authorization and payments are not
actions here; they stay behind their own dashboard buttons.

Stale drafts: a draft records the job's scope (lines, concern, status, vehicle) and the
customer's newest message when it was based. Apply re-checks both under a row lock. If either
changed, the draft becomes "stale" with the reasons, Apply is refused, the original labor
evidence is kept, and one advisor task (external id "assist:<id>") says what to do.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException

from app import assist_guard, next_action, owner_focus, pricing
from app.assist_model import ACTIVE, OPEN, AssistEvent, AssistRequest, LaborEvidence, ShopRule
from app.models import LineItem, RepairOrder

ESTIMATE_ROLES = ("owner", "admin", "office")
NOTE_ROLES = ("owner", "admin", "office", "tech")
TASK_ROLES = ("owner",)  # Master Tasks is the owner's list
ADVISOR_TASK_SOURCE = "ask_imperial"


def enabled() -> bool:
    return os.getenv("ASK_IMPERIAL_ENABLED", "").strip().lower() == "true"


def utc_now():
    return datetime.now(timezone.utc)


def event(db, actor: str, action: str, detail=None, request_id=None, ro_id=None):
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, sort_keys=True, default=str)
    db.add(AssistEvent(request_id=request_id, repair_order_id=ro_id, actor=actor[:64], action=action[:32],
                       detail=(detail or None) and detail[:2000]))


# ── Understanding the request ────────────────────────────────────────────────
ESTIMATE_WORDS = re.compile(r"\b(estimate|quote|price|pricing|how much|labor|labour|book time|draft)\b", re.I)
NOTE_WORDS = re.compile(r"^\s*(?:add\s+(?:a\s+|an\s+)?(?:internal\s+)?note|note)\b[:\s,-]*", re.I)
TASK_WORDS = re.compile(r"\b(task|to-?do)\s*#?\s*(\d{1,6})\b|\b(?:mark|close|finish)\b.*\btask\b", re.I)
TASK_DONE = re.compile(r"\b(done|finished|complete[d]?|close[d]?|handled)\b", re.I)
TASK_WAIT = re.compile(r"\b(waiting|on hold|hold off|blocked)\b", re.I)
TASK_TOMORROW = re.compile(r"\btomorrow\b", re.I)


def intent(text: str) -> str:
    text = text or ""
    if NOTE_WORDS.search(text):
        return "note"
    if TASK_WORDS.search(text):
        return "task"
    if ESTIMATE_WORDS.search(text):
        return "estimate"
    return "find"


def ro_label(ro) -> dict:
    data = owner_focus.summary(ro)
    return {"roId": ro.id, "label": data["label"], "customer": data["customer"], "vehicle": data["vehicle"],
            "status": ro.status}


def target(db, text: str, context_ro_id: int | None) -> dict:
    """The job a request is about. Inside a job, that job, unless the text names a different
    one. Two jobs named, or none found: ask, never guess."""
    focus = owner_focus.resolve(db, text or "")
    if focus.get("ro") and (context_ro_id is None or focus["ro"]["roId"] == context_ro_id or focus.get("how") == "explicit"):
        return {"ro": db.get(RepairOrder, focus["ro"]["roId"])}
    if focus.get("candidates") and not context_ro_id:
        return {"choose": focus["candidates"], "missingRos": focus.get("missingRos", [])}
    if context_ro_id:
        ro = db.get(RepairOrder, context_ro_id)
        if ro is not None:
            return {"ro": ro}
    if focus.get("missing"):
        return {"missing": focus["missing"]}
    return {}


def find_job(db, ro, role: str) -> dict:
    step = next_action.for_job(db, ro, role) or {}
    return {"job": ro_label(ro), "next": step}


# ── Internal notes ───────────────────────────────────────────────────────────
def note_text(text: str) -> str:
    body = NOTE_WORDS.sub("", text or "", count=1)
    body = re.sub(r"^\s*(?:to|on|for)\s+(?:ro|job|repair order)\s*#?\s*\d+\s*[:,-]?\s*", "", body, flags=re.I)
    return body.strip()[:2000]


def note_preview(ro, text: str) -> dict:
    body = note_text(text)
    if len(body) < 2:
        raise HTTPException(422, "Write the note after the word note.")
    return {"action": "note", "job": ro_label(ro), "change": f"Add internal note: “{body}”", "note": body,
            "visibility": "Internal: staff only. Not sent to the customer."}


def apply_note(db, ro_id: int, body: str, user) -> str:
    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).with_for_update().first()
    if ro is None:
        raise HTTPException(404, "That job no longer exists.")
    who = user.full_name or user.username
    stamp = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    ro.tech_notes = ((ro.tech_notes or "").rstrip() + f"\n\n[{stamp} · {who} · Ask Imperial] {body.strip()}").strip()
    event(db, f"user:{user.id}", "note_added", {"chars": len(body)}, ro_id=ro.id)
    db.commit()
    return f"/ro/{ro.id}#notes"


# ── Tasks ────────────────────────────────────────────────────────────────────
def task_preview(db, text: str) -> dict:
    from app.imperial_task_model import ImperialTask
    match = re.search(r"\b(?:task|to-?do)\s*#?\s*(\d{1,6})\b", text or "", re.I)
    if not match:
        raise HTTPException(422, "Say which task, for example “task 118 done”.")
    task = db.get(ImperialTask, int(match.group(1)))
    if task is None:
        raise HTTPException(404, f"Task #{match.group(1)} wasn't found.")
    if TASK_DONE.search(text):
        operation, words = "complete", "Mark done"
    elif TASK_WAIT.search(text):
        operation, words = "wait", "Move to Waiting"
    elif TASK_TOMORROW.search(text):
        operation, words = "tomorrow", "Remind tomorrow at 9 AM"
    else:
        raise HTTPException(422, "Say what to do with it: done, waiting, or tomorrow.")
    return {"action": "task", "taskId": task.id, "operation": operation,
            "change": f"{words}: task #{task.id} “{task.title}”", "current": task.status}


def apply_task(db, task_id: int, operation: str, user) -> str:
    from uuid import uuid4
    from app import task_board
    from app.routes import imperial_tasks
    when = task_board.tomorrow_morning() if operation == "tomorrow" else None
    steps = [("reopen", None), ("reschedule", when)] if operation == "tomorrow" else [(operation, None)]
    for step, due in steps:
        imperial_tasks.change_task(db, task_id, imperial_tasks.OwnerTaskChange(
            messageId=uuid4().hex, operation=step, dueAt=due), f"owner:{user.id}")
    event(db, f"user:{user.id}", "task_updated", {"taskId": task_id, "operation": operation})
    db.commit()
    return "/dashboard/tasks"


# ── Estimate drafts: scope fingerprint and staleness ─────────────────────────
def scope(ro) -> dict:
    return {
        "status": ro.status, "vehicle_id": ro.vehicle_id, "concern": (ro.concern or "").strip(),
        "lines": sorted([{"id": li.id, "description": li.description, "type": li.item_type, "qty": li.quantity,
                          "price": li.unit_price, "approval": li.approval} for li in ro.line_items],
                        key=lambda line: line["id"]),
    }


def fingerprint(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _digits(value):
    return "".join(c for c in (value or "") if c.isdigit())[-10:]


def newest_customer_message(db, ro) -> tuple[str | None, str | None]:
    """(Quo message id, its summary) of the newest message from this job's customer."""
    from app.dispatch_model import DispatchEvent, DispatchItem
    customer = ro.customer
    phone = _digits(customer.phone if customer else None)
    rows = (db.query(DispatchEvent, DispatchItem).join(DispatchItem, DispatchEvent.item_id == DispatchItem.id)
            .order_by(DispatchEvent.id.desc()).limit(300).all())
    for ev, item in rows:
        if (customer and item.customer_id == customer.id) or (phone and _digits(item.customer_phone) == phone):
            return ev.quo_message_id, (item.summary or "")[:240]
    return None, None


def what_changed(db, request: AssistRequest, ro) -> list[str]:
    reasons = []
    before = json.loads(request.base_summary or "{}")
    now = scope(ro)
    if fingerprint(now) != request.base_fingerprint:
        old_lines = {l["id"]: l for l in before.get("lines", [])}
        new_lines = {l["id"]: l for l in now["lines"]}
        for line_id in new_lines.keys() - old_lines.keys():
            reasons.append(f"Line added: “{new_lines[line_id]['description']}”")
        for line_id in old_lines.keys() - new_lines.keys():
            reasons.append(f"Line removed: “{old_lines[line_id]['description']}”")
        for line_id in new_lines.keys() & old_lines.keys():
            if new_lines[line_id] != old_lines[line_id]:
                reasons.append(f"Line changed: “{new_lines[line_id]['description']}”")
        if before.get("concern") != now["concern"]:
            reasons.append("The job's concern was edited")
        if before.get("status") != now["status"]:
            reasons.append(f"Job status changed from {before.get('status')} to {now['status']}")
        if before.get("vehicle_id") != now["vehicle_id"]:
            reasons.append("The job's vehicle changed")
        if not reasons:
            reasons.append("The job's scope changed")
    message_id, said = newest_customer_message(db, ro)
    if message_id and message_id != request.base_customer_message:
        reasons.append(f"The customer wrote after this draft was started: “{said}”")
    return reasons


def mark_stale(db, request: AssistRequest, reasons: list[str], actor: str) -> None:
    """Stale is sticky: the advisor revises the draft from the same screen. One advisor task."""
    request.status, request.stale_reason, request.updated_at = "stale", json.dumps(reasons), utc_now()
    event(db, actor, "stale", reasons, request_id=request.id, ro_id=request.repair_order_id)
    advisor_task(db, request, "Revise estimate draft: " + "; ".join(reasons)[:150])


def advisor_task(db, request: AssistRequest, title: str) -> None:
    """Exactly one Master Tasks item per draft, however often it goes stale or is retried."""
    from app.imperial_task_model import ImperialTask, ImperialTaskAudit
    external = f"assist:{request.id}"
    ro = db.get(RepairOrder, request.repair_order_id)
    label = f"RO #{ro.id} {owner_focus.summary(ro)['vehicle']}".strip() if ro else f"RO #{request.repair_order_id}"
    task = db.query(ImperialTask).filter(ImperialTask.source == ADVISOR_TASK_SOURCE,
                                         ImperialTask.external_id == external).first()
    full = f"{label}: {title}"[:200]
    if task is None:
        task = ImperialTask(source=ADVISOR_TASK_SOURCE, external_id=external, title=full, category="customer",
                            priority="normal", status="open",
                            details=json.dumps({"assistRequestId": request.id, "roId": request.repair_order_id,
                                                "link": f"/ask/requests/{request.id}"}))
        db.add(task)
        db.flush()
        db.add(ImperialTaskAudit(task_id=task.id, actor="ask_imperial", action="created", event_id=external))
    else:
        task.title, task.status, task.updated_at = full, "open", utc_now()


def close_advisor_task(db, request: AssistRequest) -> None:
    from app.imperial_task_model import ImperialTask, ImperialTaskAudit
    task = db.query(ImperialTask).filter(ImperialTask.source == ADVISOR_TASK_SOURCE,
                                         ImperialTask.external_id == f"assist:{request.id}").first()
    if task is not None and task.status != "done_verified":
        task.status, task.updated_at = "done_verified", utc_now()
        db.add(ImperialTaskAudit(task_id=task.id, actor="ask_imperial", action="complete",
                                 event_id=f"assist-done:{request.id}:{utc_now().timestamp()}"))


# ── Estimate drafts: start, evidence, price, apply ───────────────────────────
def shop_rules(db) -> list[str]:
    return [r.text for r in db.query(ShopRule).filter(ShopRule.active.is_(True), ShopRule.topic == "estimates")
            .order_by(ShopRule.id).limit(10)]


def start_estimate(db, ro, text: str, user) -> AssistRequest:
    from sqlalchemy.exc import IntegrityError
    existing = db.query(AssistRequest).filter(AssistRequest.repair_order_id == ro.id,
                                              AssistRequest.status.in_(OPEN)).first()
    if existing:
        return existing
    message_id, _ = newest_customer_message(db, ro)
    snapshot = assist_guard.build_snapshot(ro, text, shop_rules(db))
    base = scope(ro)
    request = AssistRequest(kind="estimate_draft", status="queued", requested_by_user_id=user.id,
                            repair_order_id=ro.id, vehicle_id=ro.vehicle_id, request_text=text.strip()[:2000],
                            source_snapshot=json.dumps(snapshot, sort_keys=True), base_fingerprint=fingerprint(base),
                            base_summary=json.dumps(base, sort_keys=True, default=str),
                            base_customer_message=message_id, prompt_version=assist_guard.PROMPT_VERSION)
    try:
        db.add(request)
        db.flush()
        event(db, f"user:{user.id}", "requested", {"chars": len(text)}, request_id=request.id, ro_id=ro.id)
        db.commit()
    except IntegrityError:
        db.rollback()  # a second tap raced the first; the unique index kept one
        request = db.query(AssistRequest).filter(AssistRequest.repair_order_id == ro.id,
                                                 AssistRequest.status.in_(OPEN)).first()
    return request


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def current_evidence(db, request_id: int) -> LaborEvidence | None:
    return (db.query(LaborEvidence).filter(LaborEvidence.request_id == request_id,
                                           LaborEvidence.superseded_by_id.is_(None))
            .order_by(LaborEvidence.id.desc()).first())


def price(evidence: LaborEvidence | None) -> dict:
    """Imperial's approved rate x verified guide hours; parts at the advisor's checked price;
    tax on parts only at the invoice rate. No markup (none is approved)."""
    from app.routes.invoices import SHOP_TAX_RATE
    if evidence is None:
        return {"status": "needs_labor", "label": "Needs labor lookup"}
    rate = money(pricing.rate_for(evidence.rate_type))
    hours = money(evidence.guide_hours)
    labor = money(hours * rate)
    parts = json.loads(evidence.parts_json or "[]")
    parts_total = money(sum(Decimal(str(p["price"])) * Decimal(str(p.get("qty", 1))) for p in parts))
    tax = money(parts_total * Decimal(str(SHOP_TAX_RATE)))
    return {"status": "ready", "rateType": evidence.rate_type, "rate": float(rate), "hours": float(hours),
            "labor": float(labor), "parts": parts, "partsTotal": float(parts_total), "taxRate": SHOP_TAX_RATE,
            "tax": float(tax), "total": float(labor + parts_total + tax)}


def parse_parts(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        name = (row.get("description") or "").strip()
        if not name:
            continue
        vendor = (row.get("vendor") or "").strip()
        if vendor not in ("AutoZonePro", "O'Reilly"):
            raise HTTPException(422, "Parts must be checked at AutoZonePro or O'Reilly.")
        try:
            value, qty = Decimal(str(row.get("price") or "")), Decimal(str(row.get("qty") or "1"))
        except Exception:
            raise HTTPException(422, f"Enter a price for {name}.")
        if value <= 0 or value > 20000 or qty <= 0 or qty > 50:
            raise HTTPException(422, f"Check the price and quantity for {name}.")
        out.append({"description": name[:120], "vendor": vendor, "partNumber": (row.get("part_number") or "").strip()[:40],
                    "price": float(money(value)), "qty": float(qty),
                    "availability": (row.get("availability") or "").strip()[:60],
                    "checkedAt": utc_now().isoformat()})
    return out


def apply_estimate(db, request_id: int, expected_updated: str, user) -> dict:
    """Adds draft lines (approval = none) to the RO. Refused when stale, already applied,
    or changed since the advisor's screen loaded. Never sends or approves anything."""
    request = db.query(AssistRequest).filter(AssistRequest.id == request_id).with_for_update().first()
    if request is None:
        raise HTTPException(404)
    actor = f"user:{user.id}"
    if request.status != "ready":
        event(db, actor, "apply_refused", {"status": request.status}, request_id=request.id, ro_id=request.repair_order_id)
        db.commit()
        raise HTTPException(409, "This draft isn't ready to apply." if request.status != "stale"
                            else "This draft is stale. Revise it first.")
    if expected_updated != request.updated_at.isoformat():
        event(db, actor, "apply_refused", "screen out of date", request_id=request.id, ro_id=request.repair_order_id)
        db.commit()
        raise HTTPException(409, "This draft changed while you were looking at it. Reload and check it again.")
    ro = db.query(RepairOrder).filter(RepairOrder.id == request.repair_order_id).with_for_update().first()
    reasons = what_changed(db, request, ro)
    if reasons:
        mark_stale(db, request, reasons, actor)
        event(db, actor, "apply_refused", {"stale": reasons}, request_id=request.id, ro_id=ro.id)
        db.commit()
        raise HTTPException(409, "Not applied: " + "; ".join(reasons))
    evidence = current_evidence(db, request.id)
    priced = price(evidence)
    source = (f"{evidence.source}: {evidence.operation} ({evidence.vehicle_config}), {priced['hours']:g} h guide time, "
              f"verified {evidence.verified_at:%Y-%m-%d} by user {evidence.verified_by_user_id}")
    lines = [LineItem(repair_order_id=ro.id, item_type="labor", quantity=priced["hours"], unit_price=priced["rate"],
                      description=f"{evidence.operation} [labor: {source}]"[:2000])]
    for part in priced["parts"]:
        lines.append(LineItem(repair_order_id=ro.id, item_type="part", quantity=part["qty"], unit_price=part["price"],
                              description=(f"{part['description']} [{part['vendor']}"
                                           f"{' ' + part['partNumber'] if part['partNumber'] else ''}"
                                           f", checked {part['checkedAt'][:10]}]")[:2000]))
    db.add_all(lines)
    db.flush()
    request.status, request.completed_at, request.updated_at = "applied", utc_now(), utc_now()
    request.applied_line_ids = json.dumps([line.id for line in lines])
    event(db, actor, "applied", {"lines": [line.id for line in lines], "total": priced["total"]},
          request_id=request.id, ro_id=ro.id)
    close_advisor_task(db, request)
    db.commit()
    return {"link": f"/ro/{ro.id}#line-items", "lines": [line.id for line in lines]}


def revise(db, request_id: int, user) -> AssistRequest:
    """From a stale draft: re-base on the job as it is now, keep the verified labor evidence,
    and go back to Needs labor (if the advisor must re-check) or Ready."""
    request = db.query(AssistRequest).filter(AssistRequest.id == request_id).with_for_update().first()
    if request is None or request.status != "stale":
        raise HTTPException(409, "Only a stale draft can be revised.")
    ro = db.get(RepairOrder, request.repair_order_id)
    base = scope(ro)
    message_id, _ = newest_customer_message(db, ro)
    request.base_fingerprint, request.base_summary = fingerprint(base), json.dumps(base, sort_keys=True, default=str)
    request.base_customer_message = message_id
    request.status = "ready" if current_evidence(db, request.id) else "needs_labor"
    request.updated_at = utc_now()
    event(db, f"user:{user.id}", "revised", {"reasons": json.loads(request.stale_reason or "[]")},
          request_id=request.id, ro_id=ro.id)
    request.stale_reason = None
    advisor_task(db, request, "Check the revised draft and apply it")
    db.commit()
    return request


def worker_rejects_if_moved_on(request: AssistRequest) -> bool:
    return request.status not in ACTIVE


def send_blocked(db, ro, user) -> str | None:
    """Refuse "Request approval" (the customer send) while this job has an Ask Imperial draft
    that is stale or has gone stale since it was based. Applied lines are the RO's own."""
    if not enabled():
        return None
    try:
        draft = db.query(AssistRequest).filter(AssistRequest.repair_order_id == ro.id,
                                               AssistRequest.status.in_(("needs_labor", "ready", "stale"))).first()
    except Exception:
        db.rollback()
        return None
    if draft is None:
        return None
    if draft.status != "stale":
        reasons = what_changed(db, draft, ro)
        if not reasons:
            return None
        mark_stale(db, draft, reasons, f"user:{user.id}")
    event(db, f"user:{user.id}", "send_refused", json.loads(draft.stale_reason or "[]"), request_id=draft.id, ro_id=ro.id)
    db.commit()
    return (f"Not sent: the Ask Imperial estimate draft for this job is out of date. "
            f"Revise it at /ask/requests/{draft.id} first.")
