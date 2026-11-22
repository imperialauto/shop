"""Anchor an automatic text that waits for the owner's Send to the customer's newest real text.

The deposit and pickup texts are drafted when a job event fires. If the customer's Quo thread
couldn't be read then, the draft has no real message id, and the messaging service refuses any
reply that isn't tied to the newest Quo message. So when the owner presses Send, read the thread
again and tie the draft to its newest message. If the thread still can't be read, or the customer
(or the shop) texted after the draft was made, nothing is queued and the owner is told why.
"""
from datetime import timezone

from app import customer_file

# Draft ids these automations make up when the thread couldn't be read.
SYNTHETIC = ("deposit:", "pickup:", "quote:", "diag:")
UNREADABLE = "The customer's texts couldn't be checked. Try again in a minute, or reply in Quo."
NEWER = "There's a newer text since this draft was made. Review it on the customer's page before sending."


def needs_anchor(item) -> bool:
    return not item.thread_message_id and (item.last_message_id or "").startswith(SYNTHETIC)


def anchor(item, fetch=None) -> str | None:
    """Sets item.thread_message_id to the newest real message. Returns a problem for the owner, or None."""
    tail = "".join(c for c in (item.customer_phone or "") if c.isdigit())[-10:]
    if len(tail) != 10:
        return UNREADABLE
    thread, problem = (fetch or customer_file._bridge)("/conversation", tail)
    messages = [m for m in (thread or {}).get("messages") or [] if m.get("id")]
    if problem is not None or not messages:
        return UNREADABLE
    newest = max(messages, key=lambda m: customer_file._utc(m.get("createdAt")) or customer_file._utc("1970-01-01"))
    sent_at = customer_file._utc(newest.get("createdAt"))
    made_at = item.created_at.astimezone(timezone.utc) if item.created_at else None
    if sent_at is None or made_at is None or sent_at > made_at:
        return NEWER
    item.thread_message_id = newest["id"]
    return None
