"""Owner-only bank feed controls on the Money page: connect a bank in Plaid's window, pull now,
choose shop or personal for each account, and remove a bank. See app/bank_feed.py."""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import bank_feed as feed
from app.bank_model import BankAccount, BankLink, BankTransaction
from app.appointments import PHOENIX
from app.database import get_db
from app.finance_model import SCOPES, FinanceAccount
from app.routes import finance
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
NOT_READY = "The bank feed isn't set up on this server yet, so nothing was saved."
NO_KEYS = "The bank feed needs its Plaid keys in Railway first."

# Plaid's window loads from cdn.plaid.com, so pages that open it allow that one source.
PLAID_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://unpkg.com https://cdn.plaid.com",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    "media-src 'self' blob: https:",
    "connect-src 'self' https://vpic.nhtsa.dot.gov https://*.plaid.com",
    "frame-src https://cdn.plaid.com",
    "form-action 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
))


def page_context(db: Session) -> dict:
    """What the Money page shows about linked banks. Never raises: a missing table just hides it."""
    context = {"bank_enabled": feed.enabled(), "bank_ready": True, "bank_links": [], "bank_recent": [], "bank_fed_ids": set()}
    if not context["bank_enabled"]:
        return context
    try:
        links = db.query(BankLink).order_by(BankLink.institution, BankLink.id).all()
        accounts = db.query(BankAccount).order_by(BankAccount.kind, BankAccount.name).all()
        recent = db.query(BankTransaction).order_by(BankTransaction.day.desc(), BankTransaction.id.desc()).limit(12).all()
    except SQLAlchemyError:
        db.rollback()
        context["bank_ready"] = False
        return context
    names = {a.id: a for a in accounts}
    context["bank_links"] = [{"link": link, "accounts": [a for a in accounts if a.link_id == link.id],
                              "synced": _when(link.last_synced_at)} for link in links]
    context["bank_recent"] = [{"txn": t, "account": names.get(t.bank_account_id)} for t in recent]
    context["bank_fed_ids"] = {a.finance_account_id for a in accounts if a.finance_account_id and a.tracked}
    return context


def _when(moment) -> str:
    """'today 6:30 AM' or 'Nov 22, 6:30 AM', in shop time."""
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone(PHOENIX)
    clock = local.strftime("%-I:%M %p")
    return f"today {clock}" if local.date() == finance.phoenix_today() else f"{local.strftime('%b %-d')}, {clock}"


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@router.post("/link-token")
async def link_token(request: Request, db: Session = Depends(get_db)):
    user = finance._owner(request, db)
    if not feed.enabled():
        return _json_error(NO_KEYS)
    data = await _body(request)
    access_token = None
    if data.get("link_id"):
        try:
            link = db.get(BankLink, int(data["link_id"]))
        except (SQLAlchemyError, TypeError, ValueError):
            db.rollback()
            return _json_error(NOT_READY)
        if link is None:
            return _json_error("That bank isn't linked any more.", 404)
        access_token = feed.unseal(link.access_token_enc)
        if not access_token:
            return _json_error("That bank link can't be read. Remove it and connect the bank again.")
    try:
        token = await run_in_threadpool(feed.PlaidClient().link_token, user.id, access_token)
    except feed.PlaidError as exc:
        return _json_error(feed.short_error(exc)[1], 502)
    return {"link_token": token}


@router.post("/connect")
async def connect(request: Request, db: Session = Depends(get_db)):
    """Plaid's window finished. A new bank sends a public token; a reconnect sends its link id."""
    finance._owner(request, db)
    if not feed.enabled():
        return _json_error(NO_KEYS)
    data = await _body(request)
    today = finance.phoenix_today()
    client = feed.PlaidClient()
    def work():
        if data.get("link_id"):
            link = db.get(BankLink, int(data["link_id"]))
            if link is None:
                return None, None, []
        else:
            public_token = str(data.get("public_token") or "")
            if not public_token.startswith("public-") or len(public_token) > 200:
                raise ValueError("no public token")
            link = feed.connect(db, client, public_token, str(data.get("institution") or "")[:120], today)
        new, problem = feed.pull_link(db, client, link, today)
        paid = [] if problem else feed.mark_bills_paid(db, new, today)
        db.commit()
        return link, problem, paid

    try:
        link, problem, paid = await run_in_threadpool(work)
    except (TypeError, ValueError):
        db.rollback()
        return _json_error("Plaid didn't send the bank back. Try Connect again.")
    except feed.PlaidError as exc:
        db.rollback()
        return _json_error(feed.short_error(exc)[1], 502)
    except SQLAlchemyError:
        db.rollback()
        return _json_error(NOT_READY, 503)
    if link is None:
        return _json_error("That bank isn't linked any more.", 404)
    notice = problem or f"{link.institution} is connected. " + " ".join(paid)
    return {"ok": True, "notice": notice.strip()}


@router.post("/pull")
async def pull(request: Request, db: Session = Depends(get_db)):
    finance._owner(request, db)
    form = await request.form()
    if not feed.enabled():
        return finance._back(form.get("window"), "", [NO_KEYS])
    try:
        result = await run_in_threadpool(feed.pull_all, db)
    except SQLAlchemyError:
        db.rollback()
        return finance._back(form.get("window"), "", [NOT_READY])
    notice = (f"Updated {result.banks} bank{'s' if result.banks != 1 else ''}: "
              f"{result.new_transactions} new transaction{'s' if result.new_transactions != 1 else ''}. "
              + " ".join(result.bills_paid)) if result.banks else ""
    return finance._back(form.get("window"), notice.strip(), result.problems)


@router.post("/accounts/{account_id}")
async def account_settings(account_id: int, request: Request, db: Session = Depends(get_db)):
    """Shop or personal, and whether the account counts at all. Its Money page account follows."""
    user = finance._owner(request, db)
    form = await request.form()
    scope = str(form.get("scope", "business"))
    tracked = str(form.get("tracked", "yes")) == "yes"
    if scope not in SCOPES:
        return finance._back(form.get("window"), "", ["Pick shop or personal."])
    try:
        account = db.get(BankAccount, account_id)
        if account is None:
            raise HTTPException(404, "No such bank account.")
        account.scope, account.tracked = scope, tracked
        target = db.get(FinanceAccount, account.finance_account_id) if account.finance_account_id else None
        if target is not None:
            target.scope, target.active = scope, tracked
            target.updated_at = datetime.now(timezone.utc)
        finance._log(db, user.username, "bank_account", f"{account.name} {scope} {'counted' if tracked else 'left out'}")
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        return finance._back(form.get("window"), "", [NOT_READY])
    shown = "shop" if scope == "business" else "personal"
    return finance._back(form.get("window"), f"{account.name} counts as {shown}." if tracked else f"{account.name} is left out of the totals.")


@router.post("/{link_id}/remove")
async def remove(link_id: int, request: Request, db: Session = Depends(get_db)):
    finance._owner(request, db)
    form = await request.form()
    if not feed.enabled():
        return finance._back(form.get("window"), "", [NO_KEYS])
    try:
        link = db.get(BankLink, link_id)
        if link is None:
            return finance._back(form.get("window"), "", ["That bank isn't linked any more."])
        name = link.institution
        await run_in_threadpool(feed.disconnect, db, feed.PlaidClient(), link)
        db.commit()
    except feed.PlaidError as exc:
        db.rollback()
        return finance._back(form.get("window"), "", [feed.short_error(exc)[1]])
    except SQLAlchemyError:
        db.rollback()
        return finance._back(form.get("window"), "", [NOT_READY])
    return finance._back(form.get("window"), f"{name} is disconnected. Its accounts keep their last balance on this page.")


@router.get("/oauth", response_class=HTMLResponse)
def oauth_return(request: Request, db: Session = Depends(get_db)):
    """Where some banks (Chase among them) send the phone back after their own sign-in page.
    The page reopens Plaid's window to finish, then returns to the Money page."""
    try:
        finance._owner(request, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/login", status_code=302)
        raise
    response = templates.TemplateResponse("finance/bank_oauth.html", {"request": request})
    response.headers["Content-Security-Policy"] = PLAID_CSP
    return response
