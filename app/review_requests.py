"""Google review text the day after pickup.

When a job is marked picked up (delivered), a review ask is scheduled for that customer.
On the next shop day, inside shop hours, one text goes out asking for a Google review,
with the owner's review link. Each customer is asked once, ever.

Nothing here runs unless the owner switches "Text a Google review request the day after
pickup" on in Settings. The link is the shop's own Ask for reviews link unless another is saved there. The text goes through the same
owner-approved reply queue as dashboard replies: the messaging service only sends it if
nothing newer is in the customer's thread, and never retries it. If the thread can't be
read, or the customer has texted since pickup, or a balance was waived, the draft waits for
the owner's YES instead of going out on its own.

The ask goes to every finished customer the same way. It never offers a discount or anything
else for a review, because Google doesn't allow incentives for reviews.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app import approval_links, business_settings, customer_file
from app.appointments import PHOENIX
from app.config import OWNER_PHONE
from app.dispatch_model import DispatchAudit, DispatchItem, DispatchOutbound, OPEN_STATUSES
from app.models import Communication, Customer, RepairOrder

SETTING = "automatic_review_text"
LINK_SETTING = "google_review_link"
KIND = "review_request"  # ledger rows: one per pickup, never sent by the messaging service itself
CATEGORY = "review_request"
DASHBOARD_REPLY = "dashboard_reply"  # the owner-approved reply queue the messaging service sends from
LATEST = timedelta(days=7)  # a pickup older than this is never asked, so switching on doesn't text old customers
DEFAULT_LINK = "https://g.page/r/CR_GDvvvSci2EBM/review"  # owner's Ask for reviews link, checked by him Nov 21 2026
PLACEHOLDER = "https://g.page/r/review"  # the old default in app/config.py, never a real link
LINK_HOSTS = {"g.page", "search.google.com", "www.google.com", "google.com", "maps.app.goo.gl", "goo.gl", "g.co"}
# Ledger rows live in dispatch_outbound, whose database check allows only queued, reserved, sent, failed
# and skipped. So: queued = waiting for the day after pickup; sent = the text went to the send queue;
# skipped with a draft (item_id) = held for the owner's YES; skipped with the ":reviewed" key = the owner
# marked them reviewed; any other skipped = never asked (too old, not eligible). This kind is never
# picked up by the messaging service, which only sends dashboard_reply rows.
WAITING = "queued"
REVIEWED_KEY = "reviewed"


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() == "true"


def valid_link(value: str | None) -> bool:
    value = (value or "").strip()
    if not value or value == PLACEHOLDER or any(c.isspace() for c in value):
        return False
    parts = urlsplit(value)
    return parts.scheme == "https" and (parts.hostname or "").lower() in LINK_HOSTS and not parts.username


def review_link() -> str | None:
    """The link saved in Settings, else the shop's own review link."""
    value = (business_settings.raw(LINK_SETTING) or "").strip() or DEFAULT_LINK
    return value if valid_link(value) else None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _prefix(customer_id: int) -> str:
    return f"review:{customer_id}:"


def _customer_rows(db: Session, customer_id: int):
    return db.query(DispatchOutbound).filter(DispatchOutbound.kind == KIND,
                                             DispatchOutbound.dedupe_key.like(_prefix(customer_id) + "%"))


def asked_before(db: Session, customer_id: int) -> bool:
    """Sent, held for the owner, or marked reviewed: never ask this customer again."""
    rows = _customer_rows(db, customer_id).filter(DispatchOutbound.status.in_(("sent", "skipped")))
    return any(row.status == "sent" or row.item_id is not None or row.dedupe_key.endswith(":" + REVIEWED_KEY)
               for row in rows)


def schedule(db: Session, ro: RepairOrder, now: datetime | None = None) -> DispatchOutbound | None:
    """Called when a job is marked picked up. Records the pickup so the ask can go out the next shop day."""
    if not enabled() or ro is None or ro.status != "delivered" or not ro.customer_id:
        return None
    if asked_before(db, ro.customer_id):
        return None
    key = f"{_prefix(ro.customer_id)}{ro.id}"
    if db.query(DispatchOutbound.id).filter_by(dedupe_key=key).first():
        return None
    now = now or datetime.now(timezone.utc)
    row = DispatchOutbound(dedupe_key=key, kind=KIND, status=WAITING, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    return row


def mark_reviewed(db: Session, customer_id: int, actor: str) -> None:
    """Owner says this customer already left a review (or shouldn't be asked). Nothing more is ever sent."""
    now = datetime.now(timezone.utc)
    for row in _customer_rows(db, customer_id).filter(DispatchOutbound.status == WAITING):
        row.status, row.updated_at = "skipped", now
    key = f"{_prefix(customer_id)}{REVIEWED_KEY}"
    row = db.query(DispatchOutbound).filter_by(dedupe_key=key).first()
    if row is None:
        db.add(DispatchOutbound(dedupe_key=key, kind=KIND, status="skipped", created_at=now, updated_at=now))
    db.add(DispatchAudit(item_id=None, actor=actor[:64], action="review_marked_done", detail=f"customer {customer_id}"))


def car_for(ro: RepairOrder) -> str:
    v = ro.vehicle
    name = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else ""
    return name or "car"


def message_for(ro: RepairOrder, link: str) -> str:
    return (f"Hey {approval_links.greeting_for(ro)}, thanks again for trusting us with your {car_for(ro)}. "
            f"If we took good care of you, would you leave us a quick Google review? "
            f"It really helps a small shop. {link} Jae")


def _owed(ro: RepairOrder) -> bool:
    return any(inv.balance > 0.009 for inv in ro.invoices if inv.status != "void")


def _waived(ro: RepairOrder) -> bool:
    return any((inv.waived_amount or 0) > 0 for inv in ro.invoices if inv.status != "void")


def _texted_since(db: Session, customer_id: int, since: datetime) -> bool:
    return db.query(Communication.id).filter(
        Communication.customer_id == customer_id, Communication.channel == "sms",
        Communication.direction == "inbound", Communication.created_at >= _utc(since).replace(tzinfo=None),
    ).first() is not None


def _close(row: DispatchOutbound, status: str, now: datetime) -> None:
    row.status, row.updated_at = status, now


def due(db: Session, now: datetime, fetch=None, allowed=None) -> DispatchItem | None:
    """Turns at most one scheduled pickup into a review text. Called from the dispatch tick."""
    from app import customer_followups

    link = review_link()
    if not enabled() or link is None or not (allowed or customer_followups.allowed_now)(now):
        return None
    today = now.astimezone(PHOENIX).date()
    rows = db.query(DispatchOutbound).filter(DispatchOutbound.kind == KIND, DispatchOutbound.status == WAITING) \
        .order_by(DispatchOutbound.created_at, DispatchOutbound.id).with_for_update().all()
    for row in rows:
        picked_up = _utc(row.created_at)
        if picked_up.astimezone(PHOENIX).date() >= today:
            continue  # the day after pickup, never the same day
        if now - picked_up > LATEST:
            _close(row, "skipped", now)  # too old to ask
            continue
        try:
            customer_id, ro_id = (int(x) for x in row.dedupe_key.removeprefix("review:").split(":"))
        except ValueError:
            _close(row, "skipped", now)
            continue
        ro = db.get(RepairOrder, ro_id)
        customer = db.get(Customer, customer_id)
        phone = approval_links.mobile(customer.phone if customer else None)
        if (ro is None or ro.customer_id != customer_id or ro.status != "delivered" or phone is None
                or phone == approval_links.mobile(OWNER_PHONE) or asked_before(db, customer_id)
                or customer_followups._history_blocks(db, customer_id)):
            _close(row, "skipped", now)
            continue
        if _owed(ro):
            continue  # wait for the balance; the pickup text asks for that, not this one
        thread, problem = (fetch or customer_file._bridge)("/conversation", phone[-10:])
        messages = (thread or {}).get("messages") or []
        newest = messages[-1].get("id") if messages else None
        # Goes out on its own only when the thread could be read and nothing calls for the owner's eye.
        automatic = (problem is None and bool(newest) and not _texted_since(db, customer_id, picked_up)
                     and not _waived(ro))
        conversation = f"REV{ro.id}"  # never starts with WEB, so the no-text website guards don't apply
        fields = dict(customer_phone=phone, customer_id=customer_id, vehicle_id=ro.vehicle_id, category=CATEGORY,
                      urgency="normal", summary=f"Job #{ro.id}: picked up, Google review request.",
                      proposed_action=f"Text the Google review request for job #{ro.id}",
                      draft_reply=message_for(ro, link),
                      last_message_id=newest or f"review:{ro.id}:{now.timestamp():.0f}",
                      thread_message_id=newest, updated_at=now)
        item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation,
                                             DispatchItem.status.in_(OPEN_STATUSES)).first()
        if item is None:
            item = DispatchItem(quo_conversation_id=conversation, created_at=now,
                                status="approved" if automatic else "needs_owner", **fields)
            db.add(item)
        else:
            for name, value in fields.items():
                setattr(item, name, value)
            item.status = "approved" if automatic else "needs_owner"
        db.flush()
        row.item_id = item.id
        _close(row, "sent" if automatic else "skipped", now)  # skipped + item_id = held for the owner
        if automatic:
            item.decided_at = now
            db.add(DispatchOutbound(dedupe_key=f"rev:{ro.id}:{item.id}:{newest}"[:160], kind=DASHBOARD_REPLY,
                                    item_id=item.id, status="queued", created_at=now, updated_at=now))
        db.add(DispatchAudit(item_id=item.id, actor="system",
                             action="review_text_queued" if automatic else "review_text_held", detail=f"job {ro.id}"))
        db.commit()
        return item
    db.commit()
    return None
