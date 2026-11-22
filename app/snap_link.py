"""Send the Snap Finance apply link when a customer clearly asks for it.

Off by default (setting automatic_snap_link). Runs when a customer text comes in. Only a short text
that plainly asks for Snap or financing, with nothing else that needs the owner (a price question,
a discount, a safety issue, an earlier message still waiting), gets the link on its own. Anything
less clear stays on the dashboard for the owner like any other text. Each customer gets the link at
most once a week. The text goes out on the same owner-approved reply queue as the deposit text, so
the messaging service still checks, right before sending, that the customer's ask is the newest
message and that nobody called since.
"""
import json
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import business_settings
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound

SETTING = "automatic_snap_link"
LINK_SETTING = "snap_apply_link"
DEFAULT_LINK = "https://snapf.in/IvkSFk7"
DASHBOARD_REPLY = "dashboard_reply"
REPEAT_DAYS = 7
MAX_LENGTH = 160

NEGATIVE = re.compile(r"\b(no|not|don'?t|do not|won'?t|never|without)\b[^.?!]{0,25}\b(snap|financ\w*|payments?|plan)\b"
                      r"|\b(snap|financing)\b[^.?!]{0,15}\b(denied|declined|didn'?t|did not|won'?t)\b", re.I)
CLEAR = re.compile(
    r"\bsnap\b"
    r"|\b(?:can|could|may|do|does|would)\s+(?:i|we|you)\s+(?:\w+\s+){0,2}(?:financ\w*|do payments|make payments|"
    r"pay (?:it )?(?:in|over|with) (?:time|payments|installments))\b"
    r"|\b(?:do|does|y'?all|you guys)\s+(?:\w+\s+){0,2}(?:offer|have|do|take)\s+(?:\w+\s+){0,1}(?:financing|payment plans?)\b"
    r"|\b(?:send|text|need|want|get)\s+(?:me\s+)?(?:the\s+|a\s+|that\s+)?(?:financ\w*|payment plan|apply)\s+(?:link|application|app)\b"
    r"|\b(?:i'?d like|i want|i need|i'?ll need|i'?m interested in)\s+(?:\w+\s+){0,2}(?:financing|a payment plan|to finance)\b",
    re.I)


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def link() -> str:
    value = (business_settings.raw(LINK_SETTING) or "").strip()
    return value if value.startswith("https://") else DEFAULT_LINK


def clear_ask(body: str) -> bool:
    text = " ".join((body or "").split())
    return bool(text) and len(text) <= MAX_LENGTH and bool(CLEAR.search(text)) and not NEGATIVE.search(text)


def message() -> str:
    return (f"Here's the link to apply for Snap Finance: {link()} Once you're approved, let me know and "
            f"we'll get you on the schedule. Jae")


def _sent_recently(db: Session, phone: str, now: datetime) -> bool:
    tail = "".join(c for c in phone if c.isdigit())[-10:]
    rows = (db.query(DispatchAudit, DispatchItem).join(DispatchItem, DispatchAudit.item_id == DispatchItem.id)
            .filter(DispatchAudit.action == "snap_link_queued",
                    DispatchAudit.created_at >= now - timedelta(days=REPEAT_DAYS)).all())
    return any("".join(c for c in (item.customer_phone or "") if c.isdigit())[-10:] == tail for _, item in rows)


def maybe_queue(db: Session, item: DispatchItem, body: str, result, recovery: bool = False) -> bool:
    """Answers a clear Snap ask on this conversation's item. True when the link was queued."""
    if recovery or not enabled() or not clear_ask(body):
        return False
    if result.urgency != "normal" or result.reasons or item.urgency != "normal":
        return False
    if set(json.loads(item.approval_reasons or "[]")) - {"customer_reply"}:
        return False  # something else in this conversation is waiting on the owner
    if not item.customer_phone or not item.last_message_id:
        return False
    now = datetime.now().astimezone()
    if _sent_recently(db, item.customer_phone, now):
        return False
    item.draft_reply = message()
    item.proposed_action = "Sent the Snap Finance link they asked for"
    item.status = "approved"
    item.decided_at = item.updated_at = now
    db.add(DispatchOutbound(dedupe_key=f"snap:{item.id}:{item.last_message_id}"[:160], kind=DASHBOARD_REPLY,
                            item_id=item.id, status="queued"))
    db.add(DispatchAudit(item_id=item.id, actor="system", action="snap_link_queued", detail=item.last_message_id[:255]))
    db.commit()
    return True
