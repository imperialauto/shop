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


def _event(db: Session, review, actor: str, action: str, detail=None):
    """Append an audit event. `review` is a DiagnosticReview or its id."""
    review_id = review if isinstance(review, int) else review.id
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, sort_keys=True)
    db.add(DiagnosticReviewEvent(review_id=review_id, actor=actor[:64], action=action[:32],
                                 detail=(detail or None) and detail[:2000]))


def _transition(db: Session, review_id: int, expect: dict, values: dict) -> bool:
    """Compare-and-swap one review: UPDATE … WHERE id AND every `expect` column
    still has the value the caller saw. Returns False when anything changed in
    between (another worker reclaimed it, staff cancelled it, …). PostgreSQL
    re-checks the WHERE clause after taking the row lock, so exactly one of two
    racing writers can win. Callers write audit events only after a win, in the
    same transaction, and roll back on a loss."""
    query = db.query(DiagnosticReview).filter(DiagnosticReview.id == review_id)
    for column, value in expect.items():
        field = getattr(DiagnosticReview, column)
        query = query.filter(field.in_(value) if isinstance(value, (tuple, list, set)) else field == value)
    return query.update(values, synchronize_session=False) == 1


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


def _lease_guard(worker_id: str, token: str) -> dict:
    return {"status": "processing", "lease_owner": worker_id, "lease_token": token}


def _retry_or_fail(review: DiagnosticReview, reason: str, retryable: bool, now) -> tuple[dict, str]:
    """Values for the next state after a failed attempt (not yet written)."""
    values = {"lease_owner": None, "lease_token": None, "lease_until": None,
              "last_error": reason[:1000], "updated_at": now}
    if retryable and review.attempt_count < review.max_attempts:
        values.update(status="retry_wait", retry_after=now + timedelta(seconds=30 * review.attempt_count))
        return values, "retry_scheduled"
    values.update(status="failed", completed_at=now)
    return values, "failed"


@worker_router.post("/claim")
def claim(payload: Claim, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    """Hand out at most one review under a lease. Every hand-out, and the
    give-up on an expired final attempt, is a compare-and-swap on the state the
    caller saw, and results must present the lease token that was issued."""
    now = utc_now()
    candidates = db.query(DiagnosticReview).filter(or_(
        DiagnosticReview.status == "queued",
        (DiagnosticReview.status == "retry_wait") & (DiagnosticReview.retry_after <= now),
        (DiagnosticReview.status == "processing") & (DiagnosticReview.lease_until < now),
    )).order_by(DiagnosticReview.created_at, DiagnosticReview.id).limit(5).all()
    seen = [(r.id, r.status, r.updated_at, r.lease_token, r.lease_owner, r.attempt_count, r.max_attempts)
            for r in candidates]
    for review_id, previous_status, previous_updated, previous_token, previous_owner, attempts, max_attempts in seen:
        expect = {"status": previous_status, "updated_at": previous_updated}
        if previous_status == "processing":
            expect["lease_token"] = previous_token
            # The worker holding it went quiet (crash, reboot, network loss).
            if attempts >= max_attempts:
                won = _transition(db, review_id, expect, {
                    "status": "failed", "completed_at": now, "updated_at": now,
                    "lease_owner": None, "lease_token": None, "lease_until": None,
                    "last_error": "Workstation stopped responding during the last allowed attempt."})
                if won:
                    _event(db, review_id, "system", "lease_expired_failed", f"previous worker {previous_owner or ''}")
                    db.commit()
                else:
                    db.rollback()  # its worker reported in, or another claimer handled it
                continue
        token = secrets.token_hex(24)
        if not _transition(db, review_id, expect, {
                "status": "processing", "lease_owner": payload.workerId, "lease_token": token,
                "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                "attempt_count": DiagnosticReview.attempt_count + 1,
                "started_at": now, "updated_at": now, "retry_after": None}):
            db.rollback()
            continue  # someone else took it first
        _event(db, review_id, f"node:{payload.workerId}",
               "reclaimed" if previous_status == "processing" else "claimed",
               f"attempt {attempts + 1} of {max_attempts}")
        db.commit()
        review = db.get(DiagnosticReview, review_id)
        snapshot = json.loads(review.source_snapshot)
        return {"task": {
            "taskId": review_id, "leaseToken": token, "leaseSeconds": LEASE_SECONDS,
            "attempt": attempts + 1, "promptVersion": review.prompt_version,
            "model": model_name(), "messages": guard.build_messages(snapshot),
            "format": guard.OUTPUT_SCHEMA, "options": {"temperature": 0.1, "num_ctx": 8192},
        }}
    return {"task": None}


LOST_LEASE = "This task is no longer leased to this worker."


def _leased(db: Session, task_id: int, worker_id: str, token: str) -> DiagnosticReview:
    """Fast, friendly pre-check. NOT the guard: the write itself re-checks the lease."""
    review = db.get(DiagnosticReview, task_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Unknown task.")
    if (review.status != "processing" or review.lease_owner != worker_id or not review.lease_token
            or not secrets.compare_digest(review.lease_token.encode(), token.encode())):
        raise HTTPException(status_code=409, detail=LOST_LEASE)
    return review


def _commit_or_lost(db: Session, won: bool):
    if not won:
        db.rollback()  # lease was reclaimed or the review cancelled while we worked
        raise HTTPException(status_code=409, detail=LOST_LEASE)
    db.commit()


@worker_router.post("/tasks/{task_id}/result")
def report_result(task_id: int, payload: Result, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    review = _leased(db, task_id, payload.workerId, payload.leaseToken)
    actor = f"node:{payload.workerId}"
    verdict = guard.validate_output(payload.output, json.loads(review.source_snapshot))
    model = (payload.model or model_name())[:96]
    now = utc_now()
    lease = _lease_guard(payload.workerId, payload.leaseToken)
    if not verdict["ok"]:
        reason = "AI output rejected: " + ", ".join(verdict["violations"])
        values, action = _retry_or_fail(review, reason, True, now)
        won = _transition(db, task_id, lease, {**values, "model_name": model})
        if won:
            _event(db, task_id, actor, "invalid_output", {"violations": verdict["violations"],
                                                          "durationMs": payload.durationMs})
            _event(db, task_id, actor, action, reason)
        _commit_or_lost(db, won)
        return {"accepted": False, "status": values["status"], "violations": verdict["violations"]}
    won = _transition(db, task_id, lease, {
        "status": "draft_ready", "draft_json": json.dumps(verdict["draft"], sort_keys=True),
        "validation_notes": json.dumps(verdict["cautions"]), "model_name": model,
        "lease_owner": None, "lease_token": None, "lease_until": None, "last_error": None,
        "completed_at": now, "updated_at": now})
    if won:
        _event(db, task_id, actor, "draft_ready", {"durationMs": payload.durationMs, "model": model})
    _commit_or_lost(db, won)
    return {"accepted": True, "status": "draft_ready"}


@worker_router.post("/tasks/{task_id}/error")
def report_error(task_id: int, payload: Failure, db: Session = Depends(get_db), _auth: bool = Depends(require_node)):
    review = _leased(db, task_id, payload.workerId, payload.leaseToken)
    actor = f"node:{payload.workerId}"
    reason = "Workstation error: " + payload.error
    values, action = _retry_or_fail(review, reason, payload.retryable, utc_now())
    won = _transition(db, task_id, _lease_guard(payload.workerId, payload.leaseToken), values)
    if won:
        _event(db, task_id, actor, "worker_error", payload.error)
        _event(db, task_id, actor, action, reason)
    _commit_or_lost(db, won)
    return {"status": values["status"]}


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
    # Synthetic cases only: also grades swap-test reasoning against the case's
    # known answer. Real job results (report_result) keep the hard rules alone
    # and still need a technician's approval.
    verdict = guard.validate_eval_case(payload.output, case)
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
    try:
        won = _transition(db, review_id, {"status": "failed", "updated_at": review.updated_at}, {
            "status": "queued", "attempt_count": 0, "retry_after": None, "completed_at": None,
            "updated_at": utc_now()})
    except IntegrityError:
        won = False  # the partial unique index: another review for this job is in progress
    if not won:
        db.rollback()
        raise HTTPException(status_code=409, detail="This review changed, or another review for this job is in progress.")
    _event(db, review_id, f"user:{user.id}", "requeued")
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}#ai-review", status_code=303)


@staff_router.post("/{ro_id}/ai-review/{review_id}/cancel")
def cancel_review(request: Request, ro_id: int, review_id: int, db: Session = Depends(get_db)):
    user = _staff(request, db)
    review = _review_for(db, ro_id, review_id)
    cancellable = ("queued", "retry_wait", "failed", "processing")
    if review.status not in cancellable:
        raise HTTPException(status_code=409, detail="This review can no longer be cancelled.")
    # Conditional on the state shown to the user: a draft that arrives in the
    # meantime is not silently cancelled, and a worker result after this wins nothing.
    if not _transition(db, review_id, {"status": review.status, "updated_at": review.updated_at}, {
            "status": "cancelled", "lease_owner": None, "lease_token": None, "lease_until": None,
            "updated_at": utc_now()}):
        db.rollback()
        raise HTTPException(status_code=409, detail="This review changed while you were looking at it. Reload and try again.")
    _event(db, review_id, f"user:{user.id}", "cancelled")
    db.commit()
    return RedirectResponse(f"/ro/{ro_id}#ai-review", status_code=303)
