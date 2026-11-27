"""Ask Imperial: the staff box, and the Local Node's side of estimate drafting.

Staff (signed in, role-checked here and in app/permissions.py):
  GET  /ask                         the box; ?ro=<id> opens it with that job selected
  POST /ask                         understand a request -> answer, or a preview to confirm
  POST /ask/note, /ask/task         apply a previewed internal note / task change
  GET  /ask/requests/<id>           an estimate draft: status, AI reading, labor evidence, price
  POST /ask/requests/<id>/evidence  advisor's ALLDATA / parts check (This job only or Save as a shop rule)
  POST /ask/requests/<id>/apply     add the draft lines to the RO (refused if stale)
  POST /ask/requests/<id>/revise    re-base a stale draft, keeping the verified evidence
  POST /ask/requests/<id>/retry | cancel
  GET  /ask/requests/<id>/state     JSON for the live status line

Workstation (X-Local-Node-Key, same credential and rules as diagnostic reviews):
  claim happens through /internal/local-node/claim; results come back to
  POST /internal/local-node/assist/<id>/result | error  with the lease token.
"""
import json
import os
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import assist, assist_guard
from app.assist_model import ACTIVE, AssistEvent, AssistRequest, LaborEvidence, ShopRule
from app.auth import get_current_user
from app.database import get_db
from app.models import RepairOrder
from app.routes import local_node
from app.templating import Jinja2Templates

router = APIRouter()
worker_router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))
MIN_WORKER_VERSION = (1, 2, 0)


# ── Worker side ──────────────────────────────────────────────────────────────
def _version(text):
    try:
        return tuple(int(p) for p in (text or "0").split(".")[:3])
    except ValueError:
        return (0,)


def claim_for_worker(db: Session, worker_id: str, now):
    if not assist.enabled():
        return None
    beat = db.get(local_node.LocalNodeHeartbeat, worker_id)
    if beat is None or _version(beat.version) < MIN_WORKER_VERSION:
        return None
    try:
        candidates = db.query(AssistRequest).filter(or_(
            AssistRequest.status == "queued",
            (AssistRequest.status == "retry_wait") & (AssistRequest.retry_after <= now),
            (AssistRequest.status == "processing") & (AssistRequest.lease_until < now),
        )).order_by(AssistRequest.created_at, AssistRequest.id).limit(5).all()
    except Exception:
        db.rollback()  # migration not applied: diagnostic work is unaffected
        return None
    seen = [(r.id, r.status, r.updated_at, r.lease_token, r.lease_owner, r.attempt_count, r.max_attempts)
            for r in candidates]
    for request_id, status, updated, token_was, owner_was, attempts, max_attempts in seen:
        expect = {"status": status, "updated_at": updated}
        if status == "processing":
            expect["lease_token"] = token_was
            if attempts >= max_attempts:
                if _swap(db, request_id, expect, {"status": "failed", "completed_at": now, "updated_at": now,
                                                  "lease_owner": None, "lease_token": None, "lease_until": None,
                                                  "last_error": "Workstation stopped responding during the last attempt."}):
                    assist.event(db, "system", "lease_expired_failed", f"previous worker {owner_was or ''}",
                                 request_id=request_id)
                    db.commit()
                else:
                    db.rollback()
                continue
        token = secrets.token_hex(24)
        if not _swap(db, request_id, expect, {
                "status": "processing", "lease_owner": worker_id, "lease_token": token,
                "lease_until": now + timedelta(seconds=local_node.LEASE_SECONDS),
                "attempt_count": AssistRequest.attempt_count + 1, "started_at": now, "updated_at": now,
                "retry_after": None}):
            db.rollback()
            continue
        assist.event(db, f"node:{worker_id}", "reclaimed" if status == "processing" else "claimed",
                     f"attempt {attempts + 1} of {max_attempts}" + (f"; previous worker {owner_was}" if owner_was else ""),
                     request_id=request_id)
        db.commit()
        request = db.get(AssistRequest, request_id)
        return {"taskId": request_id, "taskType": "assist_estimate", "leaseToken": token,
                "leaseSeconds": local_node.LEASE_SECONDS, "attempt": attempts + 1,
                "promptVersion": request.prompt_version, "model": local_node.model_name(),
                "messages": assist_guard.build_messages(json.loads(request.source_snapshot)),
                "format": assist_guard.OUTPUT_SCHEMA, "options": {"temperature": 0.1, "num_ctx": 8192},
                "resultPath": f"/assist/{request_id}/result", "errorPath": f"/assist/{request_id}/error"}
    return None


def _swap(db, request_id, expect, values) -> bool:
    query = db.query(AssistRequest).filter(AssistRequest.id == request_id)
    for column, value in expect.items():
        query = query.filter(getattr(AssistRequest, column) == value)
    return query.update(values, synchronize_session=False) == 1


def _refuse_late(db, request_id: int, worker_id: str, why: str):
    """A result from a worker that no longer holds the lease (it restarted, or the job moved on)
    is recorded and refused. It never changes the draft."""
    db.rollback()
    if db.get(AssistRequest, request_id) is None:
        raise HTTPException(404, "Unknown task.")
    assist.event(db, f"node:{worker_id}", "late_result_refused", why, request_id=request_id)
    db.commit()
    raise HTTPException(409, local_node.LOST_LEASE)


def _leased(db, request_id, worker_id, token) -> AssistRequest:
    request = db.get(AssistRequest, request_id)
    if request is None:
        raise HTTPException(404, "Unknown task.")
    if (request.status != "processing" or request.lease_owner != worker_id or not request.lease_token
            or not secrets.compare_digest(request.lease_token.encode(), token.encode())):
        _refuse_late(db, request_id, worker_id, f"status {request.status}; lease held by {request.lease_owner or 'nobody'}")
    return request


@worker_router.post("/assist/{request_id}/result")
def worker_result(request_id: int, payload: local_node.Result, db: Session = Depends(get_db),
                  _auth: bool = Depends(local_node.require_node)):
    request = _leased(db, request_id, payload.workerId, payload.leaseToken)
    actor, now = f"node:{payload.workerId}", assist.utc_now()
    lease = {"status": "processing", "lease_owner": payload.workerId, "lease_token": payload.leaseToken}
    verdict = assist_guard.validate(payload.output)
    model = (payload.model or local_node.model_name())[:96]
    if not verdict["ok"]:
        reason = "AI output rejected: " + ", ".join(verdict["violations"])
        retry = request.attempt_count < request.max_attempts
        values = {"lease_owner": None, "lease_token": None, "lease_until": None, "last_error": reason,
                  "updated_at": now, "model_name": model,
                  **({"status": "retry_wait", "retry_after": now + timedelta(seconds=30 * request.attempt_count)}
                     if retry else {"status": "failed", "completed_at": now})}
        if not _swap(db, request_id, lease, values):
            _refuse_late(db, request_id, payload.workerId, "lease changed while validating")
        assist.event(db, actor, "invalid_output", {"violations": verdict["violations"]}, request_id=request_id)
        db.commit()
        return {"accepted": False, "status": values["status"], "violations": verdict["violations"]}
    ro = db.get(RepairOrder, request.repair_order_id)
    reasons = assist.what_changed(db, request, ro)
    values = {"status": "stale" if reasons else "needs_labor", "ai_json": json.dumps(verdict["reading"], sort_keys=True),
              "model_name": model, "lease_owner": None, "lease_token": None, "lease_until": None,
              "last_error": None, "updated_at": now, "stale_reason": json.dumps(reasons) if reasons else None}
    if not _swap(db, request_id, lease, values):
        _refuse_late(db, request_id, payload.workerId, "lease changed while saving")
    assist.event(db, actor, "reading_ready", {"durationMs": payload.durationMs, "model": model}, request_id=request_id)
    db.flush()
    request = db.get(AssistRequest, request_id)
    if reasons:
        assist.event(db, "system", "stale", reasons, request_id=request_id, ro_id=request.repair_order_id)
        assist.advisor_task(db, request, "Revise estimate draft: " + "; ".join(reasons)[:150])
    else:
        assist.advisor_task(db, request, "Needs labor lookup in ALLDATA")
    db.commit()
    return {"accepted": True, "status": values["status"]}


@worker_router.post("/assist/{request_id}/error")
def worker_error(request_id: int, payload: local_node.Failure, db: Session = Depends(get_db),
                 _auth: bool = Depends(local_node.require_node)):
    request = _leased(db, request_id, payload.workerId, payload.leaseToken)
    now = assist.utc_now()
    retry = payload.retryable and request.attempt_count < request.max_attempts
    values = {"lease_owner": None, "lease_token": None, "lease_until": None, "updated_at": now,
              "last_error": ("Workstation error: " + payload.error)[:1000],
              **({"status": "retry_wait", "retry_after": now + timedelta(seconds=30 * request.attempt_count)}
                 if retry else {"status": "failed", "completed_at": now})}
    if not _swap(db, request_id, {"status": "processing", "lease_owner": payload.workerId,
                                  "lease_token": payload.leaseToken}, values):
        _refuse_late(db, request_id, payload.workerId, "lease changed while reporting an error")
    assist.event(db, f"node:{payload.workerId}", "worker_error", payload.error[:500], request_id=request_id)
    if values["status"] == "failed":
        assist.advisor_task(db, db.get(AssistRequest, request_id), "AI reading failed; retry or enter labor by hand")
    db.commit()
    return {"status": values["status"]}


# ── Staff side ───────────────────────────────────────────────────────────────
def _staff(request: Request, db: Session):
    if not assist.enabled():
        raise HTTPException(503, "Ask Imperial is not switched on yet.")
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


STATUS_TEXT = {
    "queued": ("Queued", "Waiting for the shop workstation."),
    "processing": ("Working", "The workstation is reading the request."),
    "retry_wait": ("Retrying", "The last try didn't work; it will try again shortly."),
    "needs_labor": ("Needs labor lookup", "Look up the operation in ALLDATA Repair and enter it below."),
    "ready": ("Needs review", "Check the draft, then apply it to the job."),
    "stale": ("Out of date", "The job or the customer changed. Revise the draft before applying."),
    "failed": ("Failed", "The workstation couldn't read it. Retry, or enter labor by hand."),
    "applied": ("Applied", "Draft lines were added to the job."),
    "cancelled": ("Cancelled", ""),
}


def worker_state(db: Session, request: AssistRequest | None = None) -> dict:
    node = local_node.node_status(db) if local_node.enabled() else {"online": False, "label": "Local Node is off"}
    out = {"online": node["online"], "label": node["label"]}
    if request is not None:
        title, detail = STATUS_TEXT.get(request.status, (request.status, ""))
        if request.status in ("queued", "retry_wait", "processing") and not node["online"]:
            title, detail = "Workstation offline", "It will pick this up when the shop PC is back. You can enter labor by hand meanwhile."
        out.update(status=request.status, title=title, detail=detail, error=request.last_error,
                   attempt=request.attempt_count, maxAttempts=request.max_attempts,
                   updated=request.updated_at.isoformat())
    return out


def _page(request: Request, user, **context):
    return templates.TemplateResponse("ask.html", {"request": request, "user": user, **context})


@router.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, ro: int | None = None, q: str = "", db: Session = Depends(get_db)):
    user = _staff(request, db)
    job = db.get(RepairOrder, ro) if ro else None
    return _page(request, user, job=assist.ro_label(job) if job else None, text=q, node=worker_state(db))


@router.post("/ask", response_class=HTMLResponse)
def ask(request: Request, text: str = Form(...), ro: str = Form(""), db: Session = Depends(get_db)):
    user = _staff(request, db)
    text = " ".join(text.split())[:2000]
    context_id = int(ro) if ro.isdigit() else None
    job = db.get(RepairOrder, context_id) if context_id else None
    base = {"job": assist.ro_label(job) if job else None, "text": text, "node": worker_state(db)}
    if len(text) < 2:
        return _page(request, user, **base, error="Type or say what you need.")
    kind = assist.intent(text)
    if kind == "task":
        if user.role not in assist.TASK_ROLES:
            return _page(request, user, **base, error="Only the owner's login can change Master Tasks.")
        try:
            return _page(request, user, **base, preview=assist.task_preview(db, text))
        except HTTPException as error:
            return _page(request, user, **base, error=error.detail)
    found = assist.target(db, text, context_id)
    if found.get("choose"):
        return _page(request, user, **base, choose=found["choose"], missing_ros=found.get("missingRos"))
    if not found.get("ro"):
        missing = found.get("missing")
        return _page(request, user, **base, error=(f"RO #{missing} wasn't found." if missing else
                                                    "I couldn't tell which job. Add the RO number, customer or vehicle."))
    ro_row = found["ro"]
    base["job"] = assist.ro_label(ro_row)
    if kind == "note":
        if user.role not in assist.NOTE_ROLES:
            raise HTTPException(403)
        try:
            return _page(request, user, **base, preview=assist.note_preview(ro_row, text))
        except HTTPException as error:
            return _page(request, user, **base, error=error.detail)
    if kind == "estimate":
        if user.role not in assist.ESTIMATE_ROLES:
            return _page(request, user, **base, error="Estimate drafts are for the office, admin or owner.")
        draft = assist.start_estimate(db, ro_row, text, user)
        return RedirectResponse(f"/ask/requests/{draft.id}", status_code=303)
    return _page(request, user, **base, answer=assist.find_job(db, ro_row, user.role))


@router.post("/ask/note")
def apply_note(request: Request, ro: int = Form(...), note: str = Form(...), db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in assist.NOTE_ROLES:
        raise HTTPException(403)
    link = assist.apply_note(db, ro, note[:2000], user)
    return RedirectResponse(f"/ask?ro={ro}&saved={link}", status_code=303)


@router.post("/ask/task")
def apply_task(request: Request, task_id: int = Form(...), operation: str = Form(...), db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in assist.TASK_ROLES or operation not in ("complete", "wait", "tomorrow"):
        raise HTTPException(403)
    link = assist.apply_task(db, task_id, operation, user)
    return RedirectResponse(f"/ask?saved={link}", status_code=303)


def _request_for(db, request_id) -> AssistRequest:
    row = db.get(AssistRequest, request_id)
    if row is None:
        raise HTTPException(404)
    return row


@router.get("/ask/requests/{request_id}", response_class=HTMLResponse)
def request_page(request: Request, request_id: int, error: str = "", db: Session = Depends(get_db)):
    user = _staff(request, db)
    row = _request_for(db, request_id)
    ro = db.get(RepairOrder, row.repair_order_id)
    evidence = assist.current_evidence(db, row.id)
    history = db.query(LaborEvidence).filter(LaborEvidence.request_id == row.id).order_by(LaborEvidence.id).all()
    events = db.query(AssistEvent).filter(AssistEvent.request_id == row.id).order_by(AssistEvent.id).all()
    return templates.TemplateResponse("ask_request.html", {
        "request": request, "user": user, "row": row, "job": assist.ro_label(ro), "ro": ro,
        "reading": json.loads(row.ai_json) if row.ai_json else None, "evidence": evidence, "history": history,
        "parts": json.loads(evidence.parts_json) if evidence else [], "priced": assist.price(evidence),
        "stale": json.loads(row.stale_reason or "[]"), "events": events, "state": worker_state(db, row),
        "can_edit": user.role in assist.ESTIMATE_ROLES, "error": error,
        "rules": [r.text for r in db.query(ShopRule).filter(ShopRule.active.is_(True)).order_by(ShopRule.id)]})


@router.get("/ask/requests/{request_id}/state")
def request_state(request: Request, request_id: int, db: Session = Depends(get_db)):
    _staff(request, db)
    return JSONResponse(worker_state(db, _request_for(db, request_id)))


@router.post("/ask/requests/{request_id}/evidence")
async def save_evidence(request: Request, request_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in assist.ESTIMATE_ROLES:
        raise HTTPException(403)
    form = await request.form()
    row = db.query(AssistRequest).filter(AssistRequest.id == request_id).with_for_update().first()
    if row is None:
        raise HTTPException(404)
    if row.status not in ("needs_labor", "ready", "failed", "queued", "retry_wait", "processing"):
        raise HTTPException(409, "This draft can't take labor evidence now." if row.status != "stale"
                            else "This draft is out of date. Revise it first.")
    if form.get("updated") != row.updated_at.isoformat():
        return RedirectResponse(f"/ask/requests/{request_id}?error=This+draft+changed.+Check+it+and+save+again.",
                                status_code=303)
    try:
        hours = float(form.get("guide_hours") or 0)
    except ValueError:
        hours = 0
    operation, config = (form.get("operation") or "").strip(), (form.get("vehicle_config") or "").strip()
    if not operation or not config or not 0 < hours <= 100:
        raise HTTPException(422, "Enter the operation, the vehicle configuration and the guide hours from ALLDATA.")
    if form.get("source") != "ALLDATA Repair" or form.get("verified") != "yes":
        raise HTTPException(422, "Confirm you checked this operation in ALLDATA Repair.")
    rate_type = form.get("rate_type") if form.get("rate_type") in ("shop", "mobile", "diesel", "cspr") else "shop"
    parts = assist.parse_parts([{k: form.get(f"part_{k}_{i}") for k in
                                 ("description", "vendor", "part_number", "price", "qty", "availability")}
                                for i in range(6)])
    old = assist.current_evidence(db, row.id)
    evidence = LaborEvidence(request_id=row.id, repair_order_id=row.repair_order_id, vehicle_config=config[:300],
                             operation=operation[:300], source="ALLDATA Repair", guide_hours=round(hours, 2),
                             inclusions=(form.get("inclusions") or "").strip()[:1000] or None,
                             overlap=(form.get("overlap") or "").strip()[:1000] or None, rate_type=rate_type,
                             verified_by_user_id=user.id,
                             shop_manager_ref=(form.get("shop_manager_ref") or "").strip()[:64] or None,
                             parts_json=json.dumps(parts))
    db.add(evidence)
    db.flush()
    if old is not None:
        old.superseded_by_id = evidence.id
    if row.status in ("queued", "retry_wait", "processing", "failed"):
        # Advisor entered labor by hand (workstation offline or failed): stop the AI attempt.
        row.lease_owner = row.lease_token = row.lease_until = None
    row.status, row.updated_at = "ready", assist.utc_now()
    assist.event(db, f"user:{user.id}", "labor_verified", {"evidenceId": evidence.id, "hours": hours,
                                                           "parts": len(parts), "rateType": rate_type},
                 request_id=row.id, ro_id=row.repair_order_id)
    rule = (form.get("rule_text") or "").strip()
    if form.get("save_as") == "shop_rule" and rule:
        db.add(ShopRule(text=rule[:1000], topic="estimates", created_by_user_id=user.id, from_request_id=row.id))
        assist.event(db, f"user:{user.id}", "shop_rule_saved", rule[:300], request_id=row.id)
    assist.advisor_task(db, row, "Check the draft and apply it")
    db.commit()
    return RedirectResponse(f"/ask/requests/{request_id}", status_code=303)


@router.post("/ask/requests/{request_id}/apply")
def apply(request: Request, request_id: int, updated: str = Form(...), db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in assist.ESTIMATE_ROLES:
        raise HTTPException(403)
    try:
        result = assist.apply_estimate(db, request_id, updated, user)
    except HTTPException as error:
        if error.status_code == 409:
            from urllib.parse import quote
            return RedirectResponse(f"/ask/requests/{request_id}?error={quote(str(error.detail))}", status_code=303)
        raise
    return RedirectResponse(f"/ask/requests/{request_id}", status_code=303)


@router.post("/ask/requests/{request_id}/revise")
def revise(request: Request, request_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in assist.ESTIMATE_ROLES:
        raise HTTPException(403)
    assist.revise(db, request_id, user)
    return RedirectResponse(f"/ask/requests/{request_id}", status_code=303)


@router.post("/ask/requests/{request_id}/retry")
def retry(request: Request, request_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    row = _request_for(db, request_id)
    if row.status != "failed" or not _swap(db, request_id, {"status": "failed", "updated_at": row.updated_at},
                                           {"status": "queued", "attempt_count": 0, "retry_after": None,
                                            "completed_at": None, "last_error": None, "updated_at": assist.utc_now()}):
        db.rollback()
        raise HTTPException(409, "Only a failed draft can be sent to the workstation again.")
    assist.event(db, f"user:{user.id}", "requeued", request_id=request_id)
    db.commit()
    return RedirectResponse(f"/ask/requests/{request_id}", status_code=303)


@router.post("/ask/requests/{request_id}/cancel")
def cancel(request: Request, request_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    row = _request_for(db, request_id)
    if row.status in ("applied", "cancelled") or not _swap(
            db, request_id, {"status": row.status, "updated_at": row.updated_at},
            {"status": "cancelled", "lease_owner": None, "lease_token": None, "lease_until": None,
             "updated_at": assist.utc_now()}):
        db.rollback()
        raise HTTPException(409, "This draft changed. Reload and try again.")
    assist.event(db, f"user:{user.id}", "cancelled", request_id=request_id)
    assist.close_advisor_task(db, row)
    db.commit()
    return RedirectResponse(f"/ro/{row.repair_order_id}", status_code=303)
