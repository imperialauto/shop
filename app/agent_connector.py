"""Claude connector: the owner signs in once and Claude gets its own assistant key.

This turns the assistant API (app/routes/agent_api.py) into a remote MCP server
that Claude can add as a custom connector. The key is created by the dashboard
and handed straight to Claude's servers through the standard OAuth sign-in, so
it never passes through a chat, a text or anyone's clipboard.

How it fits together:

* GET  /.well-known/oauth-protected-resource   tells Claude where to sign in.
* GET  /.well-known/oauth-authorization-server describes the sign-in endpoints.
* POST /internal/agent/oauth/register          Claude registers itself. Only Claude's own
                                               callback addresses are accepted, so a code can
                                               never be sent to another site.
* GET  /connect/authorize                      the owner, signed in with password and two-step
                                               code as usual, sees what Claude is asking for
                                               and taps Allow or Deny.
* POST /internal/agent/oauth/token             Claude trades the one-time code (with PKCE) for
                                               an assistant key. The key is stored hashed, shows
                                               in Settings as "Claude connector" and can be
                                               revoked there like any other key.
* POST /internal/agent/mcp                     the tools Claude can call, limited to the key's
                                               scopes, with every change logged in agent_actions.

No new tables: client registrations and one-time codes are signed with SECRET_KEY
instead of stored. Everything here is off unless AGENT_API_ENABLED=true.
"""
import base64
import hashlib
import html
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeSerializer, URLSafeTimedSerializer
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app import agent_keys, board_agent
from app.auth import get_current_user
from app.config import SECRET_KEY
from app.database import get_db
from app.routes import agent_api
from app.routes.account_security import client_key
from app.routes.appointments import AppointmentInput
from app.security import limiter
from app.security_controls import CSP

log = logging.getLogger("uvicorn.error")

CONNECTOR_NAME = "Claude connector"
MCP_PATH = "/internal/agent/mcp"
# Claude's own OAuth callback addresses. A code is only ever sent to one of these.
# The consent form must be allowed to hand the browser back to Claude; the site-wide
# policy only lets forms post to this dashboard, so Allow did nothing (Nov 21).
CONSENT_CSP = CSP.replace("form-action 'self'", "form-action 'self' https://claude.ai https://claude.com") + "; frame-ancestors 'none'"

ALLOWED_REDIRECTS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
CODE_SECONDS = 300
MCP_CONTENT = "__mcp_content__"  # a tool returning {MCP_CONTENT: [...]} sends those MCP content blocks as is
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_clients = URLSafeSerializer(SECRET_KEY, salt="imperial-agent-oauth-client")
_codes = URLSafeTimedSerializer(SECRET_KEY, salt="imperial-agent-oauth-code")
_used_codes: dict[str, float] = {}
_used_lock = threading.Lock()

public_router = APIRouter()   # /.well-known/...
staff_router = APIRouter()    # /connect/authorize (owner session)
api_router = APIRouter()      # /internal/agent/... (no session)


def _require_enabled():
    if not agent_keys.enabled():
        raise HTTPException(404, "Not found")


def base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
    return f"{proto}://{host}"


def _first_use(nonce: str) -> bool:
    """A one-time code works once. Codes expire after five minutes, so the memory stays small."""
    now = time.time()
    with _used_lock:
        for old in [n for n, at in _used_codes.items() if now - at > CODE_SECONDS * 2]:
            _used_codes.pop(old, None)
        if nonce in _used_codes:
            return False
        _used_codes[nonce] = now
        return True


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _client(client_id: str | None) -> dict | None:
    try:
        data = _clients.loads(client_id or "")
    except BadSignature:
        return None
    uris = data.get("redirect_uris") if isinstance(data, dict) else None
    if not uris or any(u not in ALLOWED_REDIRECTS for u in uris):
        return None
    return data


# ---------------------------------------------------------------- discovery

def _resource_metadata(request: Request) -> dict:
    base = base_url(request)
    return {"resource": base + MCP_PATH, "authorization_servers": [base],
            "scopes_supported": list(agent_keys.SCOPES), "bearer_methods_supported": ["header"],
            "resource_name": "Imperial Operations"}


@public_router.get("/.well-known/oauth-protected-resource")
@public_router.get("/.well-known/oauth-protected-resource/internal/agent/mcp")
def protected_resource(request: Request):
    _require_enabled()
    return _resource_metadata(request)


@public_router.get("/.well-known/oauth-authorization-server")
@public_router.get("/.well-known/oauth-authorization-server/internal/agent/mcp")
def authorization_server(request: Request):
    _require_enabled()
    base = base_url(request)
    return {"issuer": base, "authorization_endpoint": base + "/connect/authorize",
            "token_endpoint": base + "/internal/agent/oauth/token",
            "registration_endpoint": base + "/internal/agent/oauth/register",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(agent_keys.SCOPES)}


# ---------------------------------------------------------------- registration

@api_router.post("/oauth/register", status_code=201)
async def register(request: Request):
    _require_enabled()
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _oauth_error("invalid_client_metadata", "Send JSON.")
    uris = body.get("redirect_uris") if isinstance(body, dict) else None
    if not isinstance(uris, list) or not uris or len(uris) > 4 or any(u not in ALLOWED_REDIRECTS for u in uris):
        return _oauth_error("invalid_redirect_uri", "Only Claude can connect to this dashboard.")
    name = str(body.get("client_name") or "Claude")[:64]
    client_id = _clients.dumps({"redirect_uris": uris, "name": name})
    return JSONResponse({"client_id": client_id, "client_name": name, "redirect_uris": uris,
                         "grant_types": ["authorization_code"], "response_types": ["code"],
                         "token_endpoint_auth_method": "none"}, status_code=201)


# ---------------------------------------------------------------- owner consent

PAGE = """<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Connect Claude</title>
<body style="background:#09090b;color:#e4e4e7;font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;padding:40px 20px">
<h1 style="font-size:22px">Let Claude use Imperial Operations?</h1>
{body}
</body>"""


def _authorize_params(source) -> dict:
    return {k: str(source.get(k) or "") for k in
            ("response_type", "client_id", "redirect_uri", "code_challenge", "code_challenge_method", "state", "scope")}


def _check_request(params: dict) -> str | None:
    client = _client(params["client_id"])
    if client is None:
        return "This connection request didn't come from Claude."
    if params["redirect_uri"] not in client["redirect_uris"]:
        return "This connection request didn't come from Claude."
    if params["response_type"] != "code" or params["code_challenge_method"] != "S256" or len(params["code_challenge"]) < 43:
        return "This connection request is incomplete. Start again from Claude."
    return None


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Only the owner can connect Claude.")
    return user


@staff_router.get("/connect/authorize", response_class=HTMLResponse)
def authorize_page(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    params = _authorize_params(request.query_params)
    try:
        _owner(request, db)
    except HTTPException as exc:
        if exc.status_code != 401 or _check_request(params):
            raise
        # The usual sign-in keeps only a short return address; this one carries the whole request.
        request.session["next"] = request.url.path + "?" + urlencode(params)
        return RedirectResponse("/login", status_code=303)
    problem = _check_request(params)
    if problem:
        return HTMLResponse(PAGE.format(body=f"<p>{html.escape(problem)}</p>"), status_code=400)
    asked = [s for s in params["scope"].split() if s in agent_keys.SCOPES] or list(agent_keys.SCOPES)
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in params.items())
    boxes = "".join(
        f'<label style="display:block;margin:10px 0"><input type="checkbox" name="scopes" value="{s}"'
        f'{" checked" if s in asked else ""}> {html.escape(label)}</label>' for s, label in agent_keys.SCOPES.items())
    days = "".join(f'<option value="{d}"{" selected" if d == agent_keys.DEFAULT_EXPIRY_DAYS else ""}>{d} days</option>'
                   for d in agent_keys.EXPIRY_DAYS)
    body = (
        '<p style="color:#a1a1aa">Claude will get its own key for this dashboard. It can only do what you tick here. '
        'It can never invoice, take payments, see finances, or change logins or settings. It texts customers only '
        'if you tick the approval link box, and then only a job\'s approval link, from the shop line. '
        'Every change it makes is listed in Settings, where you can turn it off any time.</p>'
        f'<form method="post" action="/connect/authorize">{hidden}{boxes}'
        f'<p>Key lasts <select name="days">{days}</select></p>'
        '<button name="decision" value="allow" style="min-height:48px;padding:0 28px;font-size:16px;font-weight:700;background:#d4a853;border:0;border-radius:10px">Allow</button> '
        '<button name="decision" value="deny" style="min-height:48px;padding:0 20px;font-size:16px;background:#27272a;color:#e4e4e7;border:0;border-radius:10px">Deny</button>'
        '</form>')
    return HTMLResponse(PAGE.format(body=body), headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                         "Content-Security-Policy": CONSENT_CSP})


@staff_router.post("/connect/authorize")
async def authorize_decision(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    owner = _owner(request, db)
    form = await request.form()
    params = _authorize_params(form)
    problem = _check_request(params)
    if problem:
        return HTMLResponse(PAGE.format(body=f"<p>{html.escape(problem)}</p>"), status_code=400)
    redirect = params["redirect_uri"]
    if form.get("decision") != "allow":
        return RedirectResponse(redirect + "?" + urlencode({"error": "access_denied", "state": params["state"]}), status_code=303)
    scopes = agent_keys.clean_scopes(form.getlist("scopes"))
    try:
        days = int(form.get("days", agent_keys.DEFAULT_EXPIRY_DAYS))
    except ValueError:
        days = 0
    if not scopes or days not in agent_keys.EXPIRY_DAYS:
        return HTMLResponse(PAGE.format(body="<p>Tick at least one thing Claude may do, then try again from Claude.</p>"), status_code=400)
    code = _codes.dumps({"c": hashlib.sha256(params["client_id"].encode()).hexdigest(), "r": redirect,
                         "p": params["code_challenge"], "s": scopes, "d": days, "u": owner.id,
                         "n": secrets.token_urlsafe(16)})
    log.info("assistant_connector_approved by_user=%s scopes=%s days=%s", owner.id, " ".join(scopes), days)
    return RedirectResponse(redirect + "?" + urlencode({"code": code, "state": params["state"]}), status_code=303)


# ---------------------------------------------------------------- token

@api_router.post("/oauth/token")
async def token(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    throttle = "agent:" + client_key(request)
    if limiter.locked(throttle):
        return _oauth_error("invalid_grant", "Too many failed attempts. Wait a few minutes.", 429)
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return _oauth_error("unsupported_grant_type", "Only authorization_code is supported.")
    try:
        data = _codes.loads(str(form.get("code", "")), max_age=CODE_SECONDS)
    except (BadSignature, SignatureExpired):
        limiter.record(throttle)
        return _oauth_error("invalid_grant", "The code is invalid or expired.")
    verifier = str(form.get("code_verifier", ""))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    client_hash = hashlib.sha256(str(form.get("client_id", "")).encode()).hexdigest()
    if (not secrets.compare_digest(challenge, data["p"]) or not secrets.compare_digest(client_hash, data["c"])
            or form.get("redirect_uri") != data["r"]):
        limiter.record(throttle)
        return _oauth_error("invalid_grant", "The code doesn't match this request.")
    if not _first_use(data["n"]):
        return _oauth_error("invalid_grant", "That code was already used.")
    # Reconnecting replaces the old connector key rather than piling up keys.
    for old in agent_keys.active_keys(db):
        if old.name == CONNECTOR_NAME:
            agent_keys.revoke(db, old.id, data["u"])
    try:
        key, raw = agent_keys.issue(db, data["u"], CONNECTOR_NAME, data["s"], data["d"])
    except ValueError as exc:
        return _oauth_error("invalid_grant", str(exc))
    log.info("assistant_key_issued id=%s prefix=%s by_user=%s via=connector", key.id, key.key_prefix, data["u"])
    seconds = int((agent_keys._aware(key.expires_at) - agent_keys.utc_now()).total_seconds())
    return JSONResponse({"access_token": raw, "token_type": "Bearer", "expires_in": seconds,
                         "scope": " ".join(key.scope_list)}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


# ---------------------------------------------------------------- MCP tools

class _Id(BaseModel):
    customer_id: int = Field(gt=0)


class _Job(BaseModel):
    job_id: int = Field(gt=0)


class _Search(BaseModel):
    query: str = Field(default="", max_length=100, description="Name, phone number, fleet name or email. Blank lists the newest.")


class _Vehicle(agent_api.VehicleIn):
    customer_id: int = Field(gt=0)


class _Lines(BaseModel):
    job_id: int = Field(gt=0)
    lines: list[agent_api.LineIn] = Field(min_length=1, max_length=60)


class _JobStatus(agent_api.JobStatusIn):
    job_id: int = Field(gt=0)


class _LineEdit(agent_api.LineUpdate):
    job_id: int = Field(gt=0)
    line_id: int = Field(gt=0, description="The line's id from get_job.")


class _LineRemove(BaseModel):
    job_id: int = Field(gt=0)
    line_id: int = Field(gt=0, description="The line's id from get_job.")


class _Empty(BaseModel):
    pass


class _CustomerUpdate(agent_api.CustomerUpdate):
    customer_id: int = Field(gt=0)


class _Decisions(BaseModel):
    since: datetime | None = Field(default=None, description="ISO time to look back to, e.g. 2026-11-22T14:00:00-07:00. "
                                   "Blank means the last 24 hours; at most 14 days back.")
    include_staff: bool = Field(default=False, description="Also list yes/no recorded by shop staff in person or by phone.")


class _SendApproval(BaseModel):
    job_id: int = Field(gt=0)
    offer_snap: bool = Field(default=False, description="Add the line offering Snap Finance.")
    message: str | None = Field(default=None, max_length=1200,
                                description="Wording of the text, signed as the owner would. Blank uses the dashboard's "
                                "standard approval text.")


class _Appointment(BaseModel):
    appointment_id: int = Field(gt=0)


def _call_update_customer(db, key, a: _CustomerUpdate):
    data = a.model_dump(exclude_unset=True)
    customer_id = data.pop("customer_id")
    return agent_api.update_customer(customer_id, agent_api.CustomerUpdate(**data), db, key)


def _call_edit_line(db, key, a: _LineEdit):
    data = a.model_dump(exclude_unset=True)
    job_id, line_id = data.pop("job_id"), data.pop("line_id")
    return agent_api.update_line(job_id, line_id, agent_api.LineUpdate(**data), db, key)


def _call_estimate_pdf(db, key, a: _Job):
    """The PDF itself as an embedded file, plus the job page's print link for the owner's own browser."""
    from app import approval_links, estimate_pdf
    from app.models import RepairOrder

    ro = db.get(RepairOrder, a.job_id)
    if ro is None:
        raise HTTPException(404, "Job not found.")
    pdf = estimate_pdf.build(ro)
    link = f"{approval_links.base_url()}/ro/{ro.id}/estimate.pdf"
    info = {"job_id": ro.id, "filename": estimate_pdf.filename(ro), "bytes": len(pdf),
            "print_link": link, "totals": estimate_pdf.totals(ro),
            "note": "The print link opens the PDF on the dashboard (staff sign-in needed) to print from the browser. "
                    "Nothing was sent to anyone."}
    return {MCP_CONTENT: [
        {"type": "text", "text": json.dumps(jsonable_encoder(info), default=str)},
        {"type": "resource", "resource": {"uri": link, "mimeType": "application/pdf",
                                          "blob": base64.b64encode(pdf).decode("ascii")}},
    ]}


def _call_search(db, key, a: _Search):
    return agent_api.search_customers(a.query, db, key)


def _call_vehicle(db, key, a: _Vehicle):
    data = a.model_dump()
    customer_id = data.pop("customer_id")
    return agent_api.add_vehicle(customer_id, agent_api.VehicleIn(**data), db, key)


TOOLS = {
    "find_customers": ("customers:read", "Find customers by name, phone, fleet name or email (up to 25).", _Search,
                       _call_search, True),
    "get_customer": ("customers:read", "One customer with their vehicles and recent jobs.", _Id,
                     lambda db, key, a: agent_api.get_customer(a.customer_id, db, key), True),
    "add_customer": ("customers:write", "Add a customer. If the phone number is already on file, returns that customer instead.",
                     agent_api.CustomerIn, lambda db, key, a: agent_api.create_customer(a, db, key), False),
    "update_customer": ("customers:write", "Rename or fill in a customer (name, email, address, notes, fleet). "
                        "Only the fields sent change; the phone number is never changed.",
                        _CustomerUpdate, _call_update_customer, False),
    "add_vehicle": ("customers:write", "Add a vehicle to a customer. A VIN already on that customer returns the existing vehicle.",
                    _Vehicle, _call_vehicle, False),
    "get_job": ("estimates:read", "One job (repair order) with its estimate lines.", _Job,
                lambda db, key, a: agent_api.get_job(a.job_id, db, key), True),
    "start_estimate": ("estimates:write", "Start a job with draft estimate lines. Nothing is sent to the customer.",
                       agent_api.EstimateIn, lambda db, key, a: agent_api.create_estimate(a, db, key), False),
    "open_job": ("estimates:write", "Open a job (repair order) for a customer's vehicle with no lines yet, "
                 "so an appointment can be booked for it. Add the vehicle first if needed; make and model may be blank.",
                 agent_api.JobIn, lambda db, key, a: agent_api.open_job(a, db, key), False),
    "add_estimate_lines": ("estimates:write", "Add draft lines to an open job that has no invoice.", _Lines,
                           lambda db, key, a: agent_api.add_lines(a.job_id, a.lines, db, key), False),
    "set_job_status": ("estimates:write", "Move a job (repair order) to another column on the job board: "
                       + ", ".join(agent_api.AGENT_STATUSES) + ". Same checks as the job page, so In Progress and "
                       "Waiting Parts need an approved line, and Complete (ready for pickup, also accepted as "
                       "ready_for_pickup) needs approved work that was diagnosing, in progress or waiting parts. "
                       "Marking Complete here sends no pickup text. Not Picked up (that can text the customer). "
                       "Optional short note on why or the next step goes in the change log. Nothing is sent to anyone.",
                       _JobStatus, lambda db, key, a: agent_api.set_job_status(
                           a.job_id, agent_api.JobStatusIn(status=a.status, note=a.note), db, key), False),
    "edit_estimate_line": ("estimates:write", "Correct a draft estimate line on a job: description, item_type, quantity, "
                           "unit_price or taxable. Only the fields sent change; totals come back recalculated and the "
                           "change is logged on the job. Only lines the customer hasn't been asked to approve yet, on a "
                           "job with no invoice that isn't finished. Nothing is sent to anyone.", _LineEdit,
                           _call_edit_line, False),
    "remove_estimate_line": ("estimates:write", "Remove a draft estimate line from a job. Same limits as "
                             "edit_estimate_line, and not a line marked done. Totals come back recalculated and the "
                             "removal is logged on the job. Nothing is sent to anyone.", _LineRemove,
                             lambda db, key, a: agent_api.remove_line(a.job_id, a.line_id, db, key), False),
    "get_estimate_pdf": ("estimates:read", "A job's estimate as a printable PDF (recommended repair, optional add-ons, "
                         "parts-only tax, 50% deposit, terms), returned as a file, plus the dashboard link that opens "
                         "it for printing. Read only; nothing is sent to anyone.", _Job, _call_estimate_pdf, True),
    "list_recent_decisions": ("estimates:read", "Jobs whose estimate lines a customer approved or declined recently "
                              "(approval link or text yes), newest first, with customer, vehicle, the lines and totals. "
                              "Read only; nothing is sent to anyone.", _Decisions,
                              lambda db, key, a: agent_api.recent_decisions(db, a.since, a.include_staff), True),
    "list_appointments": ("appointments:read", "The appointment calendar.", _Empty,
                          lambda db, key, a: agent_api.list_appointments(db, key), True),
    "book_appointment": ("appointments:write", "Book a tentative appointment for a job (open_job first if there is none), "
                         "within shop hours and without double-booking.",
                         AppointmentInput, lambda db, key, a: agent_api.book_appointment(a, db, key), False),
    "list_board_todos": ("board", "The shop TV to-do list: open to-dos and those ticked off since a time, each with "
                         "its kind (alert from a customer, task from the owner's Tasks page, or manual), its task_key "
                         "and when it was ticked off.", board_agent.BoardList,
                         lambda db, key, a: board_agent.list_todos(db, a), True),
    "add_board_todo": ("board", "Add a to-do to the shop TV. With task_key, adding the same task again returns the "
                       "one already there. It shows on the TV, so no phone numbers or full customer names.",
                       board_agent.BoardAdd, lambda db, key, a: board_agent.add_todo(db, key, a), False),
    "set_board_todo": ("board", "Tick a shop TV to-do off (it moves to Completed) or put it back, by todo_id or task_key. "
                       "Ticking off a customer alert also clears it from Needs attention; alerts can't be put back.",
                       board_agent.BoardSet, lambda db, key, a: board_agent.set_todo(db, key, a), False),
    "send_approval_request": ("estimates:send", "Text the customer their approval link for a job from the shop line, "
                              "the same as the owner pressing Ask for approval on the dashboard, so the text is logged on "
                              "the job. Only when the owner has asked for it to go. Optional message is the text's wording; "
                              "the job's link is added if it isn't in it. Won't send twice: refuses if their link already "
                              "went out (from anywhere) and they haven't replied since, or if texting is paused.",
                              _SendApproval, lambda db, key, a: agent_api.send_approval(
                                  a.job_id, agent_api.SendApprovalIn(offer_snap=a.offer_snap, message=a.message), db, key),
                              False),
    "confirm_appointment": ("appointments:confirm", "Confirm a tentative appointment, only after the owner has said yes. "
                            "It then shows on his calendar feed and adds his reminder task. Nothing is sent to the customer.",
                            _Appointment, lambda db, key, a: agent_api.confirm_appointment(a.appointment_id, db, key), False),
}


def _tool_list(key) -> list[dict]:
    allowed = set(key.scope_list)
    return [{"name": name, "description": desc, "inputSchema": model.model_json_schema(),
             "annotations": {"readOnlyHint": read_only, "destructiveHint": False}}
            for name, (scope, desc, model, _, read_only) in TOOLS.items() if scope in allowed]


def _text(payload, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(jsonable_encoder(payload), default=str)}], "isError": error}


def _call_tool(db, key, params: dict) -> dict:
    name = params.get("name")
    if name not in TOOLS:
        return _text({"error": f"Unknown tool {name}."}, True)
    scope, _, model, run, _ = TOOLS[name]
    if scope not in key.scope_list:
        return _text({"error": f"This connection is not allowed to {agent_keys.SCOPES[scope].lower()}."}, True)
    try:
        args = model.model_validate(params.get("arguments") or {})
        result = run(db, key, args)
        if isinstance(result, dict) and MCP_CONTENT in result:
            return {"content": result[MCP_CONTENT], "isError": False}
        return _text(result)
    except ValidationError as exc:
        return _text({"error": "Check the details sent.", "problems": json.loads(exc.json(include_url=False))}, True)
    except HTTPException as exc:
        db.rollback()
        return _text({"error": exc.detail, "status": exc.status_code}, True)


def _unauthorized(request: Request, message: str) -> JSONResponse:
    meta = base_url(request) + "/.well-known/oauth-protected-resource"
    return JSONResponse({"error": "invalid_token", "error_description": message}, status_code=401,
                        headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})


def _rpc(id_, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": id_}
    body["error" if error else "result"] = error or result
    return JSONResponse(body)


@api_router.post("/mcp")
async def mcp(request: Request, db: Session = Depends(get_db)):
    _require_enabled()
    throttle = "agent:" + client_key(request)
    if limiter.locked(throttle):
        return JSONResponse({"error": "Too many failed key attempts. Wait a few minutes."}, status_code=429)
    key = agent_keys.find(db, agent_api._presented(request.headers.get("authorization"), None))
    if key is None:
        limiter.record(throttle)
        return _unauthorized(request, "Missing, expired or revoked connection. Reconnect from Claude.")
    try:
        message = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _rpc(None, error={"code": -32700, "message": "Parse error"})
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc(None, error={"code": -32600, "message": "Invalid request"})
    method, id_ = message.get("method"), message.get("id")
    if id_ is None:  # notifications need no answer
        return Response(status_code=202)
    key.last_used_at = agent_keys.utc_now()
    db.commit()
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        return _rpc(id_, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "imperial-operations", "version": "1.0.0"},
                          "instructions": "Imperial Auto Care's shop dashboard. Look a customer up before adding one. "
                                          "Estimates are drafts until staff send them; nothing here contacts customers."})
    if method == "ping":
        return _rpc(id_, {})
    if method == "tools/list":
        return _rpc(id_, {"tools": _tool_list(key)})
    if method == "tools/call":
        return _rpc(id_, _call_tool(db, key, params))
    return _rpc(id_, error={"code": -32601, "message": f"Method not found: {method}"})


@api_router.get("/mcp")
@api_router.delete("/mcp")
def mcp_no_stream():
    return Response(status_code=405, headers={"Allow": "POST"})
