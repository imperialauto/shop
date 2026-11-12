"""Imperial Local Node: AI diagnostic reviews on the shop workstation.

Two routers live here:

* ``worker_router`` (/internal/local-node) — called ONLY by the workstation
  worker over HTTPS with its own key (X-Local-Node-Key = LOCAL_NODE_WORKER_KEY).
  That key is separate from INTERNAL_API_KEY and can do exactly four things:
  heartbeat, claim one queued review, report a draft, report an error. It
  cannot read customers, jobs, invoices or approve anything. The workstation
  initiates every connection; nothing on it is exposed to the internet.

* ``staff_router`` (/ro/<id>/ai-review...) — signed-in staff request a review
  from a job, watch it, and a technician (tech/admin/owner) edits and approves
  or rejects the draft. Approval is the only path that writes to the job.

Everything is inert unless LOCAL_NODE_ENABLED=true (and the migration has been
applied). Nothing here sends anything to a customer.
"""
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import diagnostic_guard as guard
from app.auth import get_current_user
from app.database import get_db
from app.local_node_model import ACTIVE_STATUSES, DiagnosticReview, DiagnosticReviewEvent, LocalNodeHeartbeat
from app.models import RepairOrder, User

worker_router = APIRouter()
staff_router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

LEASE_SECONDS = 15 * 60
OFFLINE_AFTER = timedelta(minutes=3)
APPROVER_ROLES = ("tech", "admin", "owner")
WORKER_ID_PATTERN = r"^[A-Za-z0-9_-]{4,64}$"


def utc_now():
    return datetime.now(timezone.utc)


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def enabled() -> bool:
    return os.getenv("LOCAL_NODE_ENABLED", "").strip().lower() == "true"


def model_name() -> str:
    return os.getenv("LOCAL_NODE_MODEL", "imperial-diagnostics:latest").strip()[:96]


def _event(db: Session, review: DiagnosticReview, actor: str, action: str, detail=None):
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, sort_keys=True)
    db.add(DiagnosticReviewEvent(review_id=review.id, actor=actor[:64], action=action[:32],
                                 detail=(detail or None) and detail[:2000]))


# --------------------------------------------------------------------------
# Worker API
# --------------------------------------------------------------------------

def require_node(x_local_node_key: str | None = Header(default=None)) -> bool:
    if not enabled():
        raise HTTPException(status_code=503, detail="Local node is not activated.")
    key = os.getenv("LOCAL_NODE_WORKER_KEY", "")
    if len(key) < 32:
        raise HTTPException(status_code=503, detail="Local node credential is not configured.")
    if not x_local_node_key or not secrets.compare_digest(key.encode(), x_local_node_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid local node credential.")
    return True


class Heartbeat(BaseModel):
    workerId: str = Field(pattern=WORKER_ID_PATTERN)
    version: str = Field(default="", max_length=32)
    model: str = Field(default="", max_length=96)
    ollamaOk: bool = False
    detail: str = Field(default="", max_length=500)


class Claim(BaseModel):
    workerId: str = Field(pattern=WORKER_ID_PATTERN)


class Result(BaseModel):
    workerId: str = Field(pattern=WORKER_ID_PATTERN)
    leaseToken: str = Field(min_length=16, max_length=64)
    model: str = Field(default="", max_length=96)
    output: str = Field(max_length=60000)
    durationMs: int | None = Field(default=None, ge=0, le=3_600_000)


class Failure(BaseModel):
    workerId: str = Field(pattern=WORKER_ID_PATTERN)
    leaseToken: str = Field(min_length=16, max_length=64)
    error: str = Field(min_length=1, max_length=1000)
    retryable: bool = True


class ValidateOnly(BaseModel):
    case: str = Field(max_length=64)
    output: str = Field(max_length=60000)


@worker_router.post("/heartbeat")
def heartbeat(payload: Heartbeat, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    row = db.get(LocalNodeHeartbeat, payload.workerId)
    if row is None:
        row = LocalNodeHeartbeat(worker_id=payload.workerId)
        db.add(row)
    row.last_seen_at = utc_now()
    row.version = payload.version or None
    row.model_name = payload.model or None
    row.ollama_ok = "yes" if payload.ollamaOk else "no"
    row.detail = payload.detail or None
    db.commit()
    return {"ok": True, "model": model_name(), "serverTime": row.last_seen_at.isoformat()}


def _give_up_or_retry(db: Session, review: DiagnosticReview, actor: str, reason: str, retryable: bool):
    now = utc_now()
    review.lease_owner = review.lease_token = None
    review.lease_until = None
    review.last_error = reason[:1000]
    review.updated_at = now
    if retryable and review.attempt_count < review.max_attempts:
        review.status = "retry_wait"
        review.retry_after = now + timedelta(seconds=30 * review.attempt_count)
        _event(db, review, actor, "retry_scheduled", reason)
    else:
        review.status = "failed"
        review.completed_at = now
        _event(db, review, actor, "failed", reason)


@worker_router.post("/claim")
def claim(payload: Claim, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    """Hand out at most one review under a lease. Safe against two workers or a
    restarted worker: every hand-out is a compare-and-swap on (id, status,
    updated_at), and results must present the lease token that was issued."""
    now = utc_now()
    candidates = db.query(DiagnosticReview).filter(or_(
        DiagnosticReview.status == "queued",
        (DiagnosticReview.status == "retry_wait") & (DiagnosticReview.retry_after <= now),
        (DiagnosticReview.status == "processing") & (DiagnosticReview.lease_until < now),
    )).order_by(DiagnosticReview.created_at, DiagnosticReview.id).limit(5).all()
    for review in candidates:
        previous_status, previous_updated = review.status, review.updated_at
        if previous_status == "processing":
            # The worker holding it went quiet (crash, reboot, network loss).
            if review.attempt_count >= review.max_attempts:
                _event(db, review, "system", "lease_expired_failed", f"previous worker {review.lease_owner or ''}")
                review.status = "failed"
                review.completed_at = now
                review.updated_at = now
                review.lease_owner = review.lease_token = None
                review.lease_until = None
                review.last_error = "Workstation stopped responding during the last allowed attempt."
                db.commit()
                continue
        token = secrets.token_hex(24)
        changed = db.query(DiagnosticReview).filter(
            DiagnosticReview.id == review.id,
            DiagnosticReview.status == previous_status,
            DiagnosticReview.updated_at == previous_updated,
        ).update({
            "status": "processing", "lease_owner": payload.workerId, "lease_token": token,
            "lease_until": now + timedelta(seconds=LEASE_SECONDS), "attempt_count": DiagnosticReview.attempt_count + 1,
            "started_at": now, "updated_at": now, "retry_after": None,
        }, synchronize_session=False)
        if changed != 1:
            db.rollback()
            continue  # someone else took it first
        db.expire(review)
        _event(db, review, f"node:{payload.workerId}",
               "reclaimed" if previous_status == "processing" else "claimed",
               f"attempt {review.attempt_count} of {review.max_attempts}")
        db.commit()
        snapshot = json.loads(review.source_snapshot)
        return {"task": {
            "taskId": review.id, "leaseToken": token, "leaseSeconds": LEASE_SECONDS,
            "attempt": review.attempt_count, "promptVersion": review.prompt_version,
            "model": model_name(), "messages": guard.build_messages(snapshot),
            "format": guard.OUTPUT_SCHEMA, "options": {"temperature": 0.1, "num_ctx": 8192},
        }}
    return {"task": None}


def _leased(db: Session, task_id: int, worker_id: str, token: str) -> DiagnosticReview:
    review = db.get(DiagnosticReview, task_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Unknown task.")
    if (review.status != "processing" or review.lease_owner != worker_id or not review.lease_token
            or not secrets.compare_digest(review.lease_token.encode(), token.encode())):
        # A stale or duplicate worker; never overwrite the current holder's work.
        raise HTTPException(status_code=409, detail="This task is no longer leased to this worker.")
    return review


@worker_router.post("/tasks/{task_id}/result")
def report_result(task_id: int, payload: Result, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    review = _leased(db, task_id, payload.workerId, payload.leaseToken)
    actor = f"node:{payload.workerId}"
    snapshot = json.loads(review.source_snapshot)
    verdict = guard.validate_output(payload.output, snapshot)
    review.model_name = (payload.model or model_name())[:96]
    if not verdict["ok"]:
        _event(db, review, actor, "invalid_output", {"violations": verdict["violations"],
                                                      "durationMs": payload.durationMs})
        _give_up_or_retry(db, review, actor, "AI output rejected: " + ", ".join(verdict["violations"]), True)
        db.commit()
        return {"accepted": False, "status": review.status, "violations": verdict["violations"]}
    now = utc_now()
    review.status = "draft_ready"
    review.draft_json = json.dumps(verdict["draft"], sort_keys=True)
    review.validation_notes = json.dumps(verdict["cautions"])
    review.lease_owner = review.lease_token = None
    review.lease_until = None
    review.last_error = None
    review.completed_at = now
    review.updated_at = now
    _event(db, review, actor, "draft_ready", {"durationMs": payload.durationMs, "model": review.model_name})
    db.commit()
    return {"accepted": True, "status": review.status}


@worker_router.post("/tasks/{task_id}/error")
def report_error(task_id: int, payload: Failure, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    review = _leased(db, task_id, payload.workerId, payload.leaseToken)
    actor = f"node:{payload.workerId}"
    _event(db, review, actor, "worker_error", payload.error)
    _give_up_or_retry(db, review, actor, "Workstation error: " + payload.error, payload.retryable)
    db.commit()
    return {"status": review.status}


@worker_router.get("/eval-cases")
def eval_cases(_auth: bool = Depends(require_node)):
    """Synthetic commissioning prompts. No database access, no customer data."""
    return {"model": model_name(), "format": guard.OUTPUT_SCHEMA, "cases": [
        {"name": c["name"], "expect": c["expect"], "messages": guard.build_messages(c["snapshot"])}
        for c in guard.EVAL_CASES]}


@worker_router.post("/validate")
def validate_only(payload: ValidateOnly, _auth: bool = Depends(require_node)):
    case = next((c for c in guard.EVAL_CASES if c["name"] == payload.case), None)
    if case is None:
        raise HTTPException(status_code=404, detail="Unknown evaluation case.")
    verdict = guard.validate_output(payload.output, case["snapshot"])
    return {"case": case["name"], "ok": verdict["ok"], "violations": verdict["violations"],
            "cautions": verdict["cautions"], "expect": case["expect"]}


# --------------------------------------------------------------------------
# Staff (dashboard) side
# --------------------------------------------------------------------------

def node_status(db: Session) -> dict:
    row = db.query(LocalNodeHeartbeat).order_by(LocalNodeHeartbeat.last_seen_at.desc()).first()
    if row is None:
        return {"online": False, "label": "Workstation has never checked in", "last_seen": None}
    seen = _aware(row.last_seen_at)
    online = utc_now() - seen <= OFFLINE_AFTER
    if online and row.ollama_ok != "yes":
        return {"online": False, "label": "Workstation online, but Ollama is not responding", "last_seen": seen}
    return {"online": online, "label": "Workstation online" if online else "Workstation offline", "last_seen": seen}


def job_panel(db: Session, ro: RepairOrder) -> dict | None:
    """Data for the existing job page. None = feature off. Never raises."""
    if not enabled():
        return None
    try:
        reviews = db.query(DiagnosticReview).filter(DiagnosticReview.repair_order_id == ro.id) \
            .order_by(DiagnosticReview.id.desc()).limit(5).all()
        return {"reviews": reviews, "node": node_status(db), "error": None,
                "active": next((r for r in reviews if r.status in ACTIVE_STATUSES), None)}
    except Exception:
        db.rollback()
        return {"reviews": [], "node": None, "active": None,
                "error": "AI review records are unavailable. Check that the Local Node migration was applied."}


def _staff(request: Request, db: Session) -> User:
    if not enabled():
        raise HTTPException(status_code=503, detail="Local AI review is not activated.")
    return get_current_user(request, db)


def _review_for(db: Session, ro_id: int, review_id: int) -> DiagnosticReview:
    review = db.get(DiagnosticReview, review_id)
    if review is None or review.repair_order_id != ro_id:
        raise HTTPException(status_code=404)
    return review


@staff_router.post("/{ro_id}/ai-review")
def request_review(request: Request, ro_id: int, codes: str = Form(""), readings: str = Form(""),
                   db: Session = Depends(get_db)):
    user = _staff(request, db)
    ro = db.get(RepairOrder, ro_id)
    if ro is None:
        raise HTTPException(status_code=404)
    parsed, problems = guard.parse_codes(codes[:2000])
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    snapshot = guard.build_snapshot(ro, parsed, readings[:4000])
    if not (snapshot["customer_reported_concern"] or snapshot["technician_notes"] or parsed or snapshot["technician_readings"]):
        raise HTTPException(status_code=422, detail="Add a concern, notes, codes or readings before requesting a review.")
    existing = db.query(DiagnosticReview).filter(DiagnosticReview.repair_order_id == ro.id,
                                                 DiagnosticReview.status.in_(ACTIVE_STATUSES)).first()
    if existing:
        return RedirectResponse(f"/ro/{ro.id}#ai-review", status_code=303)
    review = DiagnosticReview(repair_order_id=ro.id, vehicle_id=ro.vehicle_id, requested_by_user_id=user.id,
                              status="queued", source_snapshot=json.dumps(snapshot, sort_keys=True),
                              prompt_version=guard.PROMPT_VERSION)
    try:
        db.add(review)
        db.flush()
        _event(db, review, f"user:{user.id}", "requested",
               {"codes": parsed, "readings_supplied": bool(snapshot["technician_readings"])})
        db.commit()
    except IntegrityError:
        db.rollback()  # a second click raced the first; the partial unique index kept one
    return RedirectResponse(f"/ro/{ro.id}#ai-review", status_code=303)


@staff_router.get("/{ro_id}/ai-review/{review_id}/status", response_class=HTMLResponse)
def review_status_fragment(request: Request, ro_id: int, review_id: int, db: Session = Depends(get_db)):
    _staff(request, db)
    review = _review_for(db, ro_id, review_id)
    return templates.TemplateResponse("repair_orders/_ai_review_status.html", {
        "request": request, "review": review, "ro_id": ro_id, "node": node_status(db)})


@staff_router.get("/{ro_id}/ai-review/{review_id}", response_class=HTMLResponse)
def view_review(request: Request, ro_id: int, review_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    review = _review_for(db, ro_id, review_id)
    ro = db.get(RepairOrder, ro_id)
    draft = json.loads(review.draft_json) if review.draft_json else None
    events = db.query(DiagnosticReviewEvent).filter(DiagnosticReviewEvent.review_id == review.id) \
        .order_by(DiagnosticReviewEvent.id).all()
    names = {u.id: (u.full_name or u.username) for u in db.query(User).all()}
    return templates.TemplateResponse("repair_orders/ai_review.html", {
        "request": request, "user": user, "ro": ro, "review": review, "draft": draft,
        "source": json.loads(review.source_snapshot), "cautions": json.loads(review.validation_notes or "[]"),
        "editable_text": guard.draft_to_text(draft) if draft else "", "events": events, "names": names,
        "can_decide": user.role in APPROVER_ROLES and review.status == "draft_ready", "node": node_status(db)})


def _decide(db: Session, ro_id: int, review_id: int, user: User, new_status: str, values: dict):
    """Compare-and-swap from draft_ready so two people cannot both decide."""
    now = utc_now()
    changed = db.query(DiagnosticReview).filter(
        DiagnosticReview.id == review_id, DiagnosticReview.repair_order_id == ro_id,
        DiagnosticReview.status == "draft_ready",
    ).update({"status": new_status, "reviewed_by_user_id": user.id, "reviewed_at": now, "updated_at": now, **values},
             synchronize_session=False)
    if changed != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="This review is not waiting for a decision.")
    return now


@staff_router.post("/{ro_id}/ai-review/{review_id}/approve")
def approve_review(request: Request, ro_id: int, review_id: int, approved_text: str = Form(""),
                   note: str = Form(""), db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="Only a technician, admin or the owner can approve findings.")
    text_value = approved_text.replace("\r\n", "\n").strip()
    if not text_value or len(text_value) > 8000:
        raise HTTPException(status_code=422, detail="Approved findings must be 1–8000 characters.")
    review = _review_for(db, ro_id, review_id)
    now = _decide(db, ro_id, review_id, user, "approved",
                  {"approved_text": text_value, "decision_note": note.strip()[:1000] or None})
    ro = db.get(RepairOrder, ro_id)
    reviewer = user.full_name or user.username
    block = (f"--- Diagnostic review #{review.id} · technician-approved by {reviewer} · "
             f"{now.strftime('%Y-%m-%d %H:%M UTC')} ---\n{text_value}\n{guard.end_marker(review.id)}")
    ro.tech_notes = ((ro.tech_notes or "").rstrip() + "\n\n" + block).strip()
    original = guard.draft_to_text(json.loads(review.draft_json)) if review.draft_json else ""
    _event(db, review, f"user:{user.id}", "approved",
           {"saved_to_job": True, "edited_by_technician": text_value != original})
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}/ai-review/{review_id}", status_code=303)


@staff_router.post("/{ro_id}/ai-review/{review_id}/reject")
def reject_review(request: Request, ro_id: int, review_id: int, note: str = Form(""), db: Session = Depends(get_db)):
    user = _staff(request, db)
    if user.role not in APPROVER_ROLES:
        raise HTTPException(status_code=403, detail="Only a technician, admin or the owner can reject findings.")
    review = _review_for(db, ro_id, review_id)
    _decide(db, ro_id, review_id, user, "rejected", {"decision_note": note.strip()[:1000] or None})
    _event(db, review, f"user:{user.id}", "rejected", note.strip()[:1000] or None)
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}/ai-review/{review_id}", status_code=303)


@staff_router.post("/{ro_id}/ai-review/{review_id}/retry")
def retry_review(request: Request, ro_id: int, review_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    review = _review_for(db, ro_id, review_id)
    if review.status != "failed":
        raise HTTPException(status_code=409, detail="Only a failed review can be sent again.")
    if db.query(DiagnosticReview).filter(DiagnosticReview.repair_order_id == ro_id,
                                         DiagnosticReview.status.in_(ACTIVE_STATUSES)).first():
        raise HTTPException(status_code=409, detail="Another review for this job is already in progress.")
    review.status = "queued"
    review.attempt_count = 0
    review.retry_after = None
    review.completed_at = None
    review.updated_at = utc_now()
    _event(db, review, f"user:{user.id}", "requeued")
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}#ai-review", status_code=303)


@staff_router.post("/{ro_id}/ai-review/{review_id}/cancel")
def cancel_review(request: Request, ro_id: int, review_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    review = _review_for(db, ro_id, review_id)
    if review.status not in ("queued", "retry_wait", "failed", "processing"):
        raise HTTPException(status_code=409, detail="This review can no longer be cancelled.")
    review.status = "cancelled"
    review.lease_owner = review.lease_token = None
    review.lease_until = None
    review.updated_at = utc_now()
    _event(db, review, f"user:{user.id}", "cancelled")
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}#ai-review", status_code=303)
