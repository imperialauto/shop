"""Keeps the dashboard's copy of the mailbox current, sorts new mail, and raises urgent alerts.

First connect: the last 30 days come in quietly (no alerts). After that, every check reads only what
changed since the last one (Gmail history) and alerts on new urgent mail. Alerts join the owner's
Needs-you queue as one item per email thread, keyed WEBMAIL<thread id>. Website items never text a
customer, and these never do either; the owner replies from the Email page.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import mail_gmail, mail_parse, mail_sort
from app.dispatch_model import OPEN_STATUSES, DispatchAudit, DispatchItem
from app.mail_model import MailAccount, MailAttachment, MailMessage, MailThread, utc_now
from app.models import Communication, Customer, RepairOrder

BACKFILL_QUERY = "newer_than:30d -in:spam -in:trash -in:drafts"
BACKFILL_LIMIT = 500
SKIP_LABELS = {"SPAM", "TRASH", "DRAFT"}
ALERT_PREFIX = "WEBMAIL"


def alert_key(thread: MailThread) -> str:
    return f"{ALERT_PREFIX}{thread.id}"


def is_mail_alert(item) -> bool:
    return (getattr(item, "quo_conversation_id", "") or "").startswith(ALERT_PREFIX)


def mail_thread_id(item) -> int | None:
    key = (getattr(item, "quo_conversation_id", "") or "")[len(ALERT_PREFIX):]
    return int(key) if is_mail_alert(item) and key.isdigit() else None


def account(db: Session) -> MailAccount | None:
    return db.query(MailAccount).filter(MailAccount.status != "disconnected").order_by(MailAccount.id).first()


def connect(db: Session, address: str, refresh_token: str, user_id: int | None) -> MailAccount:
    row = db.query(MailAccount).filter(func.lower(MailAccount.address) == address.lower()).first()
    if row is None:
        row = MailAccount(address=address.lower())
        db.add(row)
    row.token_encrypted = mail_gmail.encrypt_token(refresh_token)
    row.status, row.last_error, row.connected_by, row.history_id = "connected", None, user_id, None
    db.flush()
    return row


def _customer_for(db: Session, address: str) -> Customer | None:
    if not address or "@" not in address:
        return None
    return (db.query(Customer).filter(func.lower(Customer.email) == address.lower())
            .order_by(Customer.id.desc()).first())


def _open_job(db: Session, customer_id: int) -> RepairOrder | None:
    from app.ro_status_policy import CLOSED_STATUSES
    return (db.query(RepairOrder).filter(RepairOrder.customer_id == customer_id,
                                         ~RepairOrder.status.in_(CLOSED_STATUSES))
            .order_by(RepairOrder.id.desc()).first())


def refresh_thread(db: Session, thread: MailThread) -> None:
    messages = (db.query(MailMessage).filter(MailMessage.thread_id == thread.id)
                .order_by(MailMessage.sent_at.asc(), MailMessage.id.asc()).all())
    if not messages:
        return
    newest = messages[-1]
    labels = [m.label_set() for m in messages]
    thread.message_count = len(messages)
    thread.last_at = newest.sent_at or newest.created_at
    thread.last_from = "Me" if newest.outgoing else (newest.from_name or newest.from_address)[:255]
    thread.snippet = newest.snippet or newest.body_text[:200]
    thread.subject = messages[0].subject or newest.subject or "(no subject)"
    thread.unread = any("UNREAD" in s for s in labels)
    thread.in_inbox = any("INBOX" in s for s in labels)
    thread.starred = any("STARRED" in s for s in labels)
    thread.updated_at = utc_now()


def store_message(db: Session, acct: MailAccount, raw: dict, alert: bool) -> MailMessage | None:
    """Saves one Gmail message (format=full). Returns None when it's a draft, spam or trash."""
    labels = set(raw.get("labelIds") or [])
    existing = db.query(MailMessage).filter_by(gmail_message_id=raw["id"]).first()
    if existing:
        existing.labels = json.dumps(sorted(labels))
        return existing
    if labels & SKIP_LABELS:
        return None
    data = mail_parse.parse_message(raw)
    outgoing = "SENT" in labels or data["from_address"] == acct.address.lower()
    thread = (db.query(MailThread).filter_by(account_id=acct.id, gmail_thread_id=data["gmail_thread_id"]).first())
    new_thread = thread is None
    if new_thread:
        thread = MailThread(account_id=acct.id, gmail_thread_id=data["gmail_thread_id"], subject=data["subject"] or "(no subject)")
        db.add(thread)
        db.flush()
    other = data["from_address"] if not outgoing else (mail_parse.addresses(data["to_addresses"]) or [""])[0]
    customer = _customer_for(db, other)
    if customer and not thread.customer_id:
        thread.customer_id = customer.id
        job = _open_job(db, customer.id)
        if job and not thread.repair_order_id:
            thread.repair_order_id = job.id
    msg = MailMessage(thread_id=thread.id, gmail_message_id=data["gmail_message_id"],
                      rfc_message_id=data["rfc_message_id"], from_name=data["from_name"],
                      from_address=data["from_address"], reply_to=data["reply_to"], to_addresses=data["to_addresses"],
                      cc_addresses=data["cc_addresses"], subject=data["subject"], sent_at=data["sent_at"],
                      snippet=data["snippet"], body_text=data["body_text"], body_html=data["body_html"],
                      labels=json.dumps(sorted(labels)), outgoing=outgoing)
    db.add(msg)
    db.flush()
    for att in data["attachments"]:
        db.add(MailAttachment(message_id=msg.id, **att))
    sort = mail_sort.sort_message(data["from_address"], data["subject"], labels, known_customer=bool(customer),
                                  outgoing=outgoing)
    if not thread.category_locked and not outgoing and (new_thread or thread.category == "other"):
        thread.category = sort.category
    if thread.customer_id:
        db.add(Communication(customer_id=thread.customer_id, repair_order_id=thread.repair_order_id,
                             direction="outbound" if outgoing else "inbound", channel="email",
                             body=f"{data['subject']}\n{(data['body_text'] or data['snippet'])[:2000]}".strip(),
                             external_id=f"gmail:{data['gmail_message_id']}"))
    refresh_thread(db, thread)
    if outgoing:
        close_alert(db, thread, "mail", "replied by email")
    elif alert and sort.urgent and "UNREAD" in labels:
        thread.urgent = True
        raise_alert(db, thread, msg, sort)
    return msg


def raise_alert(db: Session, thread: MailThread, msg: MailMessage, sort) -> DispatchItem:
    who = msg.from_name or msg.from_address
    summary = f"{who}: {msg.subject or '(no subject)'}"
    if msg.snippet:
        summary += f" · {msg.snippet}"
    key = alert_key(thread)
    item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == key,
                                         DispatchItem.status.in_(OPEN_STATUSES)).first()
    now = datetime.now().astimezone()
    if item is None:
        customer = db.get(Customer, thread.customer_id) if thread.customer_id else None
        item = DispatchItem(quo_conversation_id=key, customer_phone=(customer.phone if customer else "") or "",
                            customer_id=thread.customer_id, contact_name=who[:120], category="email",
                            urgency="urgent" if sort.category in ("uhaul", "bills") else "normal",
                            summary="", proposed_action="", draft_reply=None, status="needs_owner",
                            last_message_id="", created_at=now)
        db.add(item)
    item.summary = summary[:280]
    item.proposed_action = f"Email · {sort.reason}. Open it in Email to reply."
    item.last_message_id = f"mail:{msg.gmail_message_id}"
    item.updated_at = now
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor="mail", action="email_alert", detail=sort.reason[:200]))
    return item


def close_alert(db: Session, thread: MailThread, actor: str, reason: str) -> int:
    rows = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == alert_key(thread),
                                         DispatchItem.status.in_(OPEN_STATUSES)).all()
    for item in rows:
        item.status, item.updated_at = "closed", datetime.now().astimezone()
        db.add(DispatchAudit(item_id=item.id, actor=actor[:64], action="email_alert_closed", detail=reason[:200]))
    thread.urgent = False
    return len(rows)


def _backfill(db: Session, acct: MailAccount, client) -> int:
    profile = client.profile()
    count = 0
    for mid in client.list_ids(BACKFILL_QUERY, BACKFILL_LIMIT):
        if db.query(MailMessage.id).filter_by(gmail_message_id=mid).first():
            continue
        if store_message(db, acct, client.get_message(mid), alert=False):
            count += 1
    acct.history_id = str(profile.get("historyId") or "")
    return count


def _apply_history(db: Session, acct: MailAccount, client, records: list) -> int:
    added, relabeled = [], {}
    for rec in records:
        for entry in rec.get("messagesAdded", []):
            added.append(entry["message"]["id"])
        for kind in ("labelsAdded", "labelsRemoved"):
            for entry in rec.get(kind, []):
                relabeled[entry["message"]["id"]] = entry["message"].get("labelIds") or []
    count, touched = 0, set()
    for mid in dict.fromkeys(added):
        stored = db.query(MailMessage).filter_by(gmail_message_id=mid).first()
        if stored:
            continue
        try:
            raw = client.get_message(mid)
        except mail_gmail.HistoryGone:
            continue  # deleted before we read it
        if store_message(db, acct, raw, alert=True):
            count += 1
    for mid, labels in relabeled.items():
        stored = db.query(MailMessage).filter_by(gmail_message_id=mid).first()
        if stored:
            stored.labels = json.dumps(sorted(set(labels)))
            touched.add(stored.thread_id)
    db.flush()
    for tid in touched:
        thread = db.get(MailThread, tid)
        refresh_thread(db, thread)
        if not thread.in_inbox:
            close_alert(db, thread, "mail", "archived in Gmail")
    return count


def sync(db: Session, acct: MailAccount | None = None) -> dict:
    """One check of the mailbox. Commits nothing; the caller commits. Errors are saved on the account."""
    acct = acct or account(db)
    if acct is None or acct.status == "disconnected":
        return {"ok": False, "reason": "not connected"}
    try:
        client = mail_gmail.client_for(acct)
        if not acct.history_id:
            new = _backfill(db, acct, client)
            mode = "backfill"
        else:
            try:
                records, newest = client.history(acct.history_id)
                new = _apply_history(db, acct, client, records)
                acct.history_id = newest
                mode = "update"
            except mail_gmail.HistoryGone:
                new = _backfill(db, acct, client)
                mode = "resync"
    except mail_gmail.MailError as exc:
        acct.status, acct.last_error = "error", str(exc)[:255]
        return {"ok": False, "reason": str(exc)}
    acct.status, acct.last_error, acct.last_sync_at = "connected", None, utc_now()
    return {"ok": True, "mode": mode, "new": new}


def set_labels(db: Session, thread: MailThread, add=(), remove=()) -> None:
    """Changes labels in Gmail first, then mirrors the change locally."""
    acct = db.get(MailAccount, thread.account_id)
    mail_gmail.client_for(acct).modify_thread(thread.gmail_thread_id, add=add, remove=remove)
    for msg in db.query(MailMessage).filter_by(thread_id=thread.id):
        labels = msg.label_set()
        labels |= set(add)
        labels -= set(remove)
        msg.labels = json.dumps(sorted(labels))
    db.flush()
    refresh_thread(db, thread)


async def scheduled_sync():
    """APScheduler job: one mailbox check every minute when MAIL_SYNC_ENABLED=true."""
    import asyncio
    await asyncio.to_thread(_scheduled_sync_blocking)


def _scheduled_sync_blocking():
    from sqlalchemy.exc import SQLAlchemyError
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        result = sync(db)
        db.commit()
        if result.get("ok") and result.get("new"):
            print(f"[Mail] {result['mode']}: {result['new']} new")
    except SQLAlchemyError as e:
        db.rollback()
        print(f"[Mail] Sync skipped: {e.__class__.__name__}")
    finally:
        db.close()


def thread_counts(db: Session, acct: MailAccount) -> dict:
    rows = (db.query(MailThread.category, func.count(MailThread.id))
            .filter(MailThread.account_id == acct.id, MailThread.in_inbox.is_(True), MailThread.unread.is_(True))
            .group_by(MailThread.category).all())
    return {cat: n for cat, n in rows}


def unread_total(db: Session) -> int:
    acct = account(db)
    if acct is None:
        return 0
    return sum(thread_counts(db, acct).values())
