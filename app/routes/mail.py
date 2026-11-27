"""The Email page: the shop's business mailbox inside the dashboard. Owner only.

Nothing sends until the owner presses Send. Mail shows as text; a message's original HTML opens only
in a sandboxed frame with scripts off and outside images held back until asked for.
"""
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app import mail_gmail, mail_parse, mail_sync
from app.auth import get_current_user
from app.database import get_db
from app.dispatch_model import DispatchAudit
from app.mail_model import CATEGORIES, MailAccount, MailAttachment, MailMessage, MailThread, enabled
from app.models import Customer, RepairOrder
from app.templating import Jinja2Templates

router = APIRouter()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "..", "templates"))

PAGE_SIZE = 30
MAX_UPLOAD = 18 * 1024 * 1024  # Gmail's limit is 25 MB after encoding
SIGNATURE = "Jae\nImperial Auto Care\n(480) 914-4144\nimperialautophx.com"
STARTERS = (
    ("Ask for details", "Thanks for reaching out. What's the year, make and model, and what's it doing? I'll get you a price today."),
    ("Offer a time", "Got it. I can get you in this week. What day and time work best for you?"),
    ("Estimate coming", "Thanks. I'm putting your estimate together now and will send it over shortly."),
    ("Got it", "Received, thank you."),
    ("Call me", "Can you give me a call at (480) 914-4144 when you get a minute?"),
)
NOTES = {
    "sent": "Sent.", "archived": "Archived.", "moved": "Moved.", "linked": "Linked.", "unlinked": "Unlinked.",
    "synced": "Checked for new mail.", "connected": "Mailbox connected. The last 30 days are coming in now.",
    "disconnected": "Mailbox disconnected. Nothing was deleted from Gmail.", "unread": "Marked unread.",
    "restored": "Moved back to the inbox.", "starred": "Starred.", "unstarred": "Star removed.",
}
# Shown in red after a redirect, when Gmail turned a change down.
ERRORS = {"gmail": "Gmail didn't take that change, so nothing moved. Try again in a minute."}

_sent_keys: dict[str, float] = {}
_sent_lock = threading.Lock()


def _first_use(key: str) -> bool:
    """One send per form. A double tap or a resubmitted page doesn't send twice."""
    now = time.time()
    with _sent_lock:
        for k, at in list(_sent_keys.items()):
            if now - at > 3600:
                del _sent_keys[k]
        if not key or key in _sent_keys:
            return False
        _sent_keys[key] = now
        return True


def _release(key: str) -> None:
    with _sent_lock:
        _sent_keys.pop(key, None)


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if user.role != "owner":
        raise HTTPException(403, "Email is owner-only.")
    return user


def _ctx(request, user, **extra):
    note = NOTES.get(request.query_params.get("note", ""))
    flash_error = ERRORS.get(request.query_params.get("err", ""))
    return {"request": request, "user": user, "note": note, "flash_error": flash_error, "categories": CATEGORIES, **extra}


def _off(request, user):
    return templates.TemplateResponse("mail/off.html", _ctx(request, user, missing=mail_gmail.setup_missing()))


def _account_or_none(db):
    return mail_sync.account(db)


def _thread(db, thread_id: int) -> MailThread:
    thread = db.get(MailThread, thread_id)
    if thread is None:
        raise HTTPException(404, "That email isn't here.")
    return thread


def _back(path: str, note: str = "") -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}note={note}" if note else path, status_code=303)


def _safe_next(value: str, default: str) -> str:
    value = value or ""
    return value if value.startswith("/mail") and not value.startswith("//") else default


def _read_only(request) -> bool:
    return bool(getattr(request.app.state, "read_only", False))


def split_quote(text: str) -> tuple[str, str]:
    """The new part of a message and the quoted earlier mail under it."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if (re.match(r"^On .{6,200}wrote:$", s) or s.startswith("-----Original Message") or
                re.match(r"^From: .+", s) and i + 1 < len(lines) and re.match(r"^(Sent|Date): ", lines[i + 1].strip())):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:])
        if s.startswith(">") and all(not x.strip() or x.strip().startswith(">") for x in lines[i:]):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:])
    return (text or "").rstrip(), ""


def _when(dt) -> str:
    from app.templating import shop_time
    if not dt:
        return ""
    local = shop_time(dt)
    now = shop_time(datetime.now(timezone.utc))
    if local.date() == now.date():
        return local.strftime("%-I:%M %p")
    if (now.date() - local.date()).days < 7:
        return local.strftime("%a %-I:%M %p")
    if local.year == now.year:
        return local.strftime("%b %-d")
    return local.strftime("%b %-d, %Y")


templates.env.filters["mail_when"] = _when


# ── Inbox ─────────────────────────────────────────────────────────────────────
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def inbox(request: Request, tab: str = "", box: str = "inbox", q: str = "", page: int = 1, gmail: int = 0,
          db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        return _off(request, user)
    acct = _account_or_none(db)
    if acct is None:
        return RedirectResponse("/mail/settings", status_code=303)
    tab = tab if tab in CATEGORIES else ""
    box = box if box in ("inbox", "starred", "sent", "all") else "inbox"
    q = q.strip()[:200]
    page = max(1, min(page, 200))
    search_error = ""
    if q and gmail:
        try:  # search the whole mailbox in Gmail, not just what's been copied in
            client = mail_gmail.client_for(acct)
            for mid in client.list_ids(q, 50):
                if not db.query(MailMessage.id).filter_by(gmail_message_id=mid).first():
                    mail_sync.store_message(db, acct, client.get_message(mid), alert=False)
            db.commit()
        except mail_gmail.MailError as exc:
            db.rollback()
            search_error = str(exc)
    query = db.query(MailThread).filter(MailThread.account_id == acct.id)
    if box == "inbox":
        query = query.filter(MailThread.in_inbox.is_(True))
    elif box == "starred":
        query = query.filter(MailThread.starred.is_(True))
    elif box == "sent":
        sent_threads = db.query(MailMessage.thread_id).filter(MailMessage.outgoing.is_(True))
        query = query.filter(MailThread.id.in_(sent_threads))
    if tab and box == "inbox":
        query = query.filter(MailThread.category == tab)
    if q:
        like = f"%{q.lower()}%"
        hits = db.query(MailMessage.thread_id).filter(or_(
            func.lower(MailMessage.subject).like(like), func.lower(MailMessage.from_name).like(like),
            func.lower(MailMessage.from_address).like(like), func.lower(MailMessage.to_addresses).like(like),
            func.lower(MailMessage.body_text).like(like)))
        query = query.filter(MailThread.id.in_(hits))
    total = query.count()
    threads = (query.order_by(MailThread.last_at.desc().nullslast(), MailThread.id.desc())
               .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1).all())
    more = len(threads) > PAGE_SIZE
    threads = threads[:PAGE_SIZE]
    customers = {c.id: c for c in db.query(Customer).filter(Customer.id.in_({t.customer_id for t in threads if t.customer_id}))} if threads else {}
    counts = mail_sync.thread_counts(db, acct)
    return templates.TemplateResponse("mail/list.html", _ctx(
        request, user, acct=acct, threads=threads, customers=customers, tab=tab, box=box, q=q, page=page,
        more=more, total=total, counts=counts, unread_all=sum(counts.values()), searched_gmail=bool(gmail),
        search_error=search_error))


# ── One thread ────────────────────────────────────────────────────────────────
@router.get("/t/{thread_id}", response_class=HTMLResponse)
def thread_view(request: Request, thread_id: int, find: str = "", db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        return _off(request, user)
    thread = _thread(db, thread_id)
    return _render_thread(request, db, user, thread, find=find)


def _render_thread(request, db, user, thread, find="", error="", draft=None, status_code=200):
    acct = db.get(MailAccount, thread.account_id)
    read_error = ""
    if thread.unread and not _read_only(request) and not error:
        try:
            mail_sync.set_labels(db, thread, remove=["UNREAD"])
            db.commit()
        except mail_gmail.MailError as exc:
            db.rollback()
            read_error = str(exc)
    messages = (db.query(MailMessage).filter_by(thread_id=thread.id)
                .order_by(MailMessage.sent_at.asc(), MailMessage.id.asc()).all())
    attachments = {}
    for att in db.query(MailAttachment).filter(MailAttachment.message_id.in_([m.id for m in messages])).all():
        attachments.setdefault(att.message_id, []).append(att)
    shown = [(m, *split_quote(m.body_text)) for m in messages]
    last_in = next((m for m in reversed(messages) if not m.outgoing), messages[-1] if messages else None)
    to = mail_parse.reply_target(last_in) if last_in and not last_in.outgoing else (last_in.to_addresses if last_in else "")
    customer = db.get(Customer, thread.customer_id) if thread.customer_id else None
    job = db.get(RepairOrder, thread.repair_order_id) if thread.repair_order_id else None
    jobs = []
    if customer:
        jobs = db.query(RepairOrder).filter(RepairOrder.customer_id == customer.id).order_by(RepairOrder.id.desc()).limit(8).all()
    matches = []
    find = (find or "").strip()[:80]
    if find:
        like = f"%{find.lower()}%"
        matches = (db.query(Customer).filter(or_(
            func.lower(Customer.first_name + " " + Customer.last_name).like(like), func.lower(Customer.email).like(like),
            Customer.phone.like(f"%{re.sub(r'[^0-9]', '', find) or find}%"), func.lower(Customer.fleet_name).like(like)))
            .order_by(Customer.id.desc()).limit(8).all())
    sender = _person_address(last_in)
    draft = draft or {}
    return templates.TemplateResponse("mail/thread.html", _ctx(
        request, user, acct=acct, thread=thread, shown=shown, attachments=attachments, customer=customer, job=job,
        jobs=jobs, find=find, matches=matches, sender=sender, error=error, read_error=read_error,
        reply_to=draft.get("to", to), reply_cc=draft.get("cc", ""), reply_body=draft.get("body", ""),
        noreply=mail_parse.is_noreply(to.split(",")[0]) if to else False,
        starters=STARTERS, signature=SIGNATURE, send_key=secrets.token_urlsafe(16),
        subject=mail_parse.reply_subject(thread.subject)), status_code=status_code)


def _person_address(message) -> str:
    """The sender's own address: the Reply-To a web form gives, else From. Empty for no-reply robots."""
    if not message or message.outgoing:
        return ""
    first = (mail_parse.reply_target(message).split(",") or [""])[0].strip()
    return "" if not first or mail_parse.is_noreply(first) else first


def _files(uploads) -> list:
    files, total = [], 0
    for up in uploads or []:
        if not up or not up.filename:
            continue
        data = up.file.read(MAX_UPLOAD + 1)
        total += len(data)
        if total > MAX_UPLOAD:
            raise ValueError("Attachments are over 18 MB. Send fewer or smaller files.")
        name = os.path.basename(up.filename)[:200] or "file"
        files.append((name, up.content_type or "application/octet-stream", data))
    return files


def _clean_recipients(value: str) -> str:
    found = mail_parse.addresses(value)
    bad = [p.strip() for p in (value or "").replace(";", ",").split(",") if p.strip() and "@" not in p]
    if bad or len(found) > 20:
        raise ValueError("Check the addresses. Each one needs an @, separated by commas.")
    return ", ".join(found)


def _signed(body: str, sign: bool) -> str:
    body = (body or "").rstrip()
    if sign and SIGNATURE.splitlines()[1] not in body:
        body = f"{body}\n\n{SIGNATURE}"
    return body + "\n"


def _send(db, acct, raw, thread_gmail_id=None):
    client = mail_gmail.client_for(acct)
    sent = client.send(raw, thread_gmail_id)
    try:
        stored = mail_sync.store_message(db, acct, client.get_message(sent["id"]), alert=False)
    except mail_gmail.MailError:
        stored = None  # it went; the next check copies it in
    return sent, stored


@router.post("/t/{thread_id}/reply")
def reply(request: Request, thread_id: int, to: str = Form(""), cc: str = Form(""), body: str = Form(""),
          sign: str = Form(""), send_key: str = Form(""), files: list[UploadFile] = File(default=[]),
          db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    thread = _thread(db, thread_id)
    acct = db.get(MailAccount, thread.account_id)
    draft = {"to": to, "cc": cc, "body": body}
    from app import sending_pause
    if sending_pause.is_paused(db):
        return _render_thread(request, db, user, thread, error="Sending is stopped (STOP ALL). Turn sending back on first.", draft=draft, status_code=409)
    if not body.strip():
        return _render_thread(request, db, user, thread, error="Write a reply first.", draft=draft, status_code=400)
    try:
        to_clean = _clean_recipients(to)
        cc_clean = _clean_recipients(cc) if cc.strip() else ""
        if not to_clean:
            raise ValueError("Add who it goes to.")
        attached = _files(files)
    except ValueError as exc:
        return _render_thread(request, db, user, thread, error=str(exc), draft=draft, status_code=400)
    if not send_key:
        return _render_thread(request, db, user, thread, error="Not sent. Reload the page and send again.", draft=draft, status_code=400)
    if not _first_use(send_key):
        return _back(f"/mail/t/{thread.id}", "sent")
    last = (db.query(MailMessage).filter_by(thread_id=thread.id)
            .order_by(MailMessage.sent_at.desc(), MailMessage.id.desc()).first())
    refs = " ".join(filter(None, [m.rfc_message_id for m in db.query(MailMessage).filter_by(thread_id=thread.id)
                                  .order_by(MailMessage.sent_at.asc()).all() if m.rfc_message_id]))[-900:]
    raw = mail_parse.build_message(acct.address, os.getenv("MAIL_SENDER_NAME", "Imperial Auto Care"), to_clean,
                                   mail_parse.reply_subject(thread.subject), _signed(body, bool(sign)), cc=cc_clean,
                                   in_reply_to=last.rfc_message_id if last else None,
                                   references=refs.replace(last.rfc_message_id or "", "").strip() if last else "",
                                   files=attached)
    try:
        _send(db, acct, raw, thread.gmail_thread_id)
    except mail_gmail.MailError as exc:
        db.rollback()
        _release(send_key)
        return _render_thread(request, db, user, thread, error=f"Not sent. {exc}", draft=draft, status_code=502)
    mail_sync.close_alert(db, thread, f"user:{user.id}", "replied by email")
    db.add(DispatchAudit(item_id=None, actor=f"user:{user.id}", action="email_sent", detail=f"thread {thread.id}"))
    db.commit()
    return _back(f"/mail/t/{thread.id}", "sent")


def _label_action(request, db, thread_id, add=(), remove=(), note="", close=False, next_url=""):
    _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    thread = _thread(db, thread_id)
    try:
        mail_sync.set_labels(db, thread, add=add, remove=remove)
    except mail_gmail.MailError:
        db.rollback()
        back = f"/mail/t/{thread.id}"
        return RedirectResponse(f"{back}?err=gmail", status_code=303)
    if close:
        mail_sync.close_alert(db, thread, "user", "archived")
    db.commit()
    return _back(_safe_next(next_url, f"/mail/t/{thread.id}"), note)


@router.post("/t/{thread_id}/archive")
def archive(request: Request, thread_id: int, next: str = Form(""), db: Session = Depends(get_db)):
    return _label_action(request, db, thread_id, remove=["INBOX"], note="archived", close=True,
                         next_url=_safe_next(next, "/mail"))


@router.post("/t/{thread_id}/inbox")
def unarchive(request: Request, thread_id: int, db: Session = Depends(get_db)):
    return _label_action(request, db, thread_id, add=["INBOX"], note="restored")


@router.post("/t/{thread_id}/unread")
def mark_unread(request: Request, thread_id: int, db: Session = Depends(get_db)):
    return _label_action(request, db, thread_id, add=["UNREAD"], note="unread", next_url="/mail")


@router.post("/t/{thread_id}/star")
def star(request: Request, thread_id: int, on: str = Form("1"), next: str = Form(""), db: Session = Depends(get_db)):
    if on == "1":
        return _label_action(request, db, thread_id, add=["STARRED"], note="starred", next_url=next)
    return _label_action(request, db, thread_id, remove=["STARRED"], note="unstarred", next_url=next)


@router.post("/t/{thread_id}/category")
def move(request: Request, thread_id: int, category: str = Form(...), db: Session = Depends(get_db)):
    _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    if category not in CATEGORIES:
        raise HTTPException(400, "Pick a tab.")
    thread = _thread(db, thread_id)
    thread.category, thread.category_locked = category, True
    db.commit()
    return _back(f"/mail/t/{thread.id}", "moved")


@router.post("/t/{thread_id}/link")
def link(request: Request, thread_id: int, customer_id: str = Form(""), repair_order_id: str = Form(""),
         save_email: str = Form(""), db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    thread = _thread(db, thread_id)
    if customer_id == "none":
        thread.customer_id = thread.repair_order_id = None
        db.commit()
        return _back(f"/mail/t/{thread.id}", "unlinked")
    customer = db.get(Customer, int(customer_id)) if customer_id.isdigit() else (
        db.get(Customer, thread.customer_id) if thread.customer_id else None)
    if customer is None:
        raise HTTPException(400, "Pick a customer.")
    job = None
    if repair_order_id.isdigit():
        job = db.get(RepairOrder, int(repair_order_id))
        if job is None or job.customer_id != customer.id:
            raise HTTPException(400, "That job isn't this customer's.")
    same = thread.customer_id == customer.id
    thread.customer_id = customer.id
    if job:
        thread.repair_order_id = job.id
    elif repair_order_id == "none" or not same:
        thread.repair_order_id = None
    if save_email:
        last_in = (db.query(MailMessage).filter_by(thread_id=thread.id, outgoing=False)
                   .order_by(MailMessage.id.desc()).first())
        address = _person_address(last_in)
        if address and not customer.email:
            customer.email = address[:128]
    db.add(DispatchAudit(item_id=None, actor=f"user:{user.id}", action="email_linked", detail=f"thread {thread.id} -> customer {customer.id}"))
    db.commit()
    return _back(f"/mail/t/{thread.id}", "linked")


# ── Attachments and original view ────────────────────────────────────────────
@router.get("/a/{attachment_id}")
def attachment(request: Request, attachment_id: int, db: Session = Depends(get_db)):
    _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    att = db.get(MailAttachment, attachment_id)
    if att is None:
        raise HTTPException(404)
    msg = db.get(MailMessage, att.message_id)
    thread = db.get(MailThread, msg.thread_id)
    acct = db.get(MailAccount, thread.account_id)
    try:
        data = mail_gmail.client_for(acct).get_attachment(msg.gmail_message_id, att.gmail_attachment_id)
    except mail_gmail.MailError as exc:
        raise HTTPException(502, f"Gmail didn't send the file. {exc}")
    safe_name = re.sub(r'[^\w.\- ]', "_", att.filename)[:150] or "attachment"
    inline = att.mime_type in ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")
    return Response(data, media_type=att.mime_type if inline else "application/octet-stream", headers={
        "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe_name}"',
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"})


@router.get("/m/{message_id}/html", response_class=HTMLResponse)
def original(request: Request, message_id: int, images: int = 0, db: Session = Depends(get_db)):
    _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    msg = db.get(MailMessage, message_id)
    if msg is None or not msg.body_html:
        raise HTTPException(404)
    img = "img-src data: https:" if images else "img-src data:"
    page = ('<!doctype html><meta charset="utf-8"><base target="_blank">'
            '<style>body{font:15px/1.5 -apple-system,system-ui,sans-serif;margin:12px;color:#222;background:#fff;'
            'overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%!important}</style>'
            + msg.body_html)
    return HTMLResponse(page, headers={
        "Content-Security-Policy": f"default-src 'none'; {img}; style-src 'unsafe-inline'; font-src data:; "
                                   "frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox allow-popups allow-popups-to-escape-sandbox",
        "X-Frame-Options": "SAMEORIGIN", "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff"})


# ── Compose ───────────────────────────────────────────────────────────────────
@router.get("/compose", response_class=HTMLResponse)
def compose_page(request: Request, to: str = "", customer_id: int = 0, db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        return _off(request, user)
    acct = _account_or_none(db)
    if acct is None:
        return RedirectResponse("/mail/settings", status_code=303)
    customer = db.get(Customer, customer_id) if customer_id else None
    if customer and customer.email and not to:
        to = customer.email
    return templates.TemplateResponse("mail/compose.html", _ctx(
        request, user, acct=acct, to=to[:500], cc="", subject="", body="", error="", customer=customer,
        starters=STARTERS, signature=SIGNATURE, send_key=secrets.token_urlsafe(16)))


@router.post("/compose")
def compose_send(request: Request, to: str = Form(""), cc: str = Form(""), subject: str = Form(""),
                 body: str = Form(""), sign: str = Form(""), send_key: str = Form(""), customer_id: str = Form(""),
                 files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    acct = _account_or_none(db)
    if acct is None:
        return RedirectResponse("/mail/settings", status_code=303)
    customer = db.get(Customer, int(customer_id)) if customer_id.isdigit() else None

    def again(error, code):
        return templates.TemplateResponse("mail/compose.html", _ctx(
            request, user, acct=acct, to=to, cc=cc, subject=subject, body=body, error=error, customer=customer,
            starters=STARTERS, signature=SIGNATURE, send_key=secrets.token_urlsafe(16)), status_code=code)

    from app import sending_pause
    if sending_pause.is_paused(db):
        return again("Sending is stopped (STOP ALL). Turn sending back on first.", 409)
    try:
        to_clean = _clean_recipients(to)
        cc_clean = _clean_recipients(cc) if cc.strip() else ""
        if not to_clean:
            raise ValueError("Add who it goes to.")
        if not subject.strip():
            raise ValueError("Add a subject.")
        if not body.strip():
            raise ValueError("Write the message first.")
        attached = _files(files)
    except ValueError as exc:
        return again(str(exc), 400)
    if not send_key:
        return again("Not sent. Reload the page and send again.", 400)
    if not _first_use(send_key):
        return _back("/mail?box=sent", "sent")
    raw = mail_parse.build_message(acct.address, os.getenv("MAIL_SENDER_NAME", "Imperial Auto Care"), to_clean,
                                   subject.strip()[:300], _signed(body, bool(sign)), cc=cc_clean, files=attached)
    try:
        sent, stored = _send(db, acct, raw)
    except mail_gmail.MailError as exc:
        db.rollback()
        _release(send_key)
        return again(f"Not sent. {exc}", 502)
    thread = db.get(MailThread, stored.thread_id) if stored else None
    if thread and customer and not thread.customer_id:
        thread.customer_id = customer.id
    db.add(DispatchAudit(item_id=None, actor=f"user:{user.id}", action="email_sent", detail="new email"))
    db.commit()
    return _back(f"/mail/t/{thread.id}" if thread else "/mail?box=sent", "sent")


# ── Settings, connect, sync ──────────────────────────────────────────────────
def _redirect_uri(request: Request) -> str:
    configured = os.getenv("MAIL_OAUTH_REDIRECT_URI", "").strip()
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/mail/oauth/callback"


@router.get("/settings", response_class=HTMLResponse)
def settings(request: Request, error: str = "", db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        return _off(request, user)
    acct = _account_or_none(db)
    errors = {"state": "That sign-in link was old or from another tab. Try Connect again.",
              "denied": "Google sign-in was cancelled. Nothing changed.", "google": request.session.pop("mail_error", "")}
    stats = {}
    if acct:
        stats = {"threads": db.query(MailThread).filter_by(account_id=acct.id).count(),
                 "messages": db.query(MailMessage).join(MailThread).filter(MailThread.account_id == acct.id).count()}
    return templates.TemplateResponse("mail/settings.html", _ctx(
        request, user, acct=acct, missing=mail_gmail.setup_missing(), error=errors.get(error, ""),
        redirect_uri=_redirect_uri(request), stats=stats,
        sync_on=os.getenv("MAIL_SYNC_ENABLED", "").strip().lower() == "true"))


@router.get("/connect")
def connect(request: Request, db: Session = Depends(get_db)):
    _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    if mail_gmail.setup_missing():
        return RedirectResponse("/mail/settings", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["mail_oauth_state"] = state
    return RedirectResponse(mail_gmail.auth_url(state, _redirect_uri(request), os.getenv("MAIL_ADDRESS", "")),
                            status_code=303)


@router.get("/oauth/callback")
def oauth_callback(request: Request, state: str = "", code: str = "", error: str = "", db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    expected = request.session.pop("mail_oauth_state", "")
    if not expected or not secrets.compare_digest(expected, state or ""):
        return RedirectResponse("/mail/settings?error=state", status_code=303)
    if error or not code:
        return RedirectResponse("/mail/settings?error=denied", status_code=303)
    try:
        token = mail_gmail.exchange_code(code, _redirect_uri(request))
        address = mail_gmail.client_from_token(token).profile().get("emailAddress", "")
        if not address:
            raise mail_gmail.MailError("Gmail didn't say which mailbox this is.")
        mail_sync.connect(db, address, token, user.id)
        db.add(DispatchAudit(item_id=None, actor=f"user:{user.id}", action="mail_connected", detail=address[:200]))
        db.commit()
    except mail_gmail.MailError as exc:
        db.rollback()
        request.session["mail_error"] = str(exc)
        return RedirectResponse("/mail/settings?error=google", status_code=303)
    return RedirectResponse("/mail/settings?note=connected", status_code=303)


@router.post("/sync")
def sync_now(request: Request, next: str = Form(""), db: Session = Depends(get_db)):
    _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    acct = _account_or_none(db)
    if acct is None:
        return RedirectResponse("/mail/settings", status_code=303)
    result = mail_sync.sync(db, acct)
    db.commit()
    if not result.get("ok"):
        request.session["mail_error"] = result.get("reason", "")
        return RedirectResponse("/mail/settings?error=google", status_code=303)
    return _back(_safe_next(next, "/mail"), "synced")


@router.post("/disconnect")
def disconnect(request: Request, db: Session = Depends(get_db)):
    user = _owner(request, db)
    if not enabled():
        raise HTTPException(404)
    acct = _account_or_none(db)
    if acct:
        acct.status, acct.token_encrypted = "disconnected", None
        db.add(DispatchAudit(item_id=None, actor=f"user:{user.id}", action="mail_disconnected", detail=acct.address[:200]))
        db.commit()
    return _back("/mail/settings", "disconnected")
