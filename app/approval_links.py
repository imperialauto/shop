"""Approval links by text.

Office staff: "Ask for approval" queues one item for the owner holding the text and
the vehicle status link. Nothing reaches the customer until the owner replies YES
(or approves it on the dashboard).

Owner or admin (and the owner's Claude connector): send_now() texts the link straight
away from the shop line. The press is the owner's yes. It goes out through the same
dashboard send path, which logs the text on the job, and the messaging service still
refuses to send if the customer has texted since the button was pressed.
"""
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import snap_offer
from app.dispatch_model import OPEN_STATUSES, DispatchAudit, DispatchItem
from app.models import RepairOrder

CATEGORY = "approval_link"


def conversation_key(ro_id: int) -> str:
    return f"LINK{ro_id}"  # never starts with WEB, so it is not treated as a website item


def base_url() -> str:
    from app import public_links
    return public_links.base_url()


def mobile(phone) -> str | None:
    digits = "".join(c for c in str(phone or "") if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return "+1" + digits if len(digits) == 10 else None


def greeting_for(ro: RepairOrder) -> str:
    """Never address a customer by an auto-imported vehicle year or placeholder."""
    parts = (ro.customer.first_name or "").strip().split()
    first = parts[0].strip(".,:-") if parts else ""
    if not first.isalpha() or first.casefold() in {"unknown", "customer", "guest", "lead", "vehicle", "new", "test"}:
        return "there"
    return first


PHOTOS_LINE = "Photos from the inspection are included."


def photos_note(ro: RepairOrder) -> str:
    """The photos sentence for the approval text, only when a photo on this job is marked for the customer."""
    from sqlalchemy.orm import object_session
    from app.models import RoAttachment
    db = object_session(ro)
    if db is None or ro.id is None:
        return ""
    shared = (db.query(RoAttachment.id).filter(RoAttachment.repair_order_id == ro.id,
                                               RoAttachment.visibility == "customer",
                                               RoAttachment.content_type.like("image/%")).first())
    return f" {PHOTOS_LINE}" if shared else ""


def message_for(ro: RepairOrder, offer_snap: bool = False) -> str:
    v = ro.vehicle
    car = " ".join(str(x) for x in (v.year, v.make, v.model) if x) if v else "vehicle"
    first = greeting_for(ro)
    text = (f"Hi {first}, your estimate for the {car} is ready. Open your vehicle status and tap Review & Approve: "
            f"{base_url()}/status/{ro.public_token}{photos_note(ro)} Review the itemized charges and any applicable tax before approving. "
            "Reply here with any questions. - Imperial Auto Care")
    return snap_offer.add(text, offer_snap)


def default_text(ro: RepairOrder, offer_snap: bool = False) -> str:
    """The standard text, or the "we updated your estimate" text when lines changed since they answered."""
    from app import line_changes
    if line_changes.has_changes(ro):
        return line_changes.message(ro, offer_snap)
    return message_for(ro, offer_snap)


def queue(db: Session, ro: RepairOrder, newest_message_id: str, actor: str,
          offer_snap: bool = False, text: str | None = None) -> DispatchItem:
    """Creates or refreshes the owner's approval item for this job. Sends nothing."""
    phone = mobile(ro.customer.phone)
    if phone is None:
        raise ValueError("no_phone")
    waiting = [li for li in ro.line_items if li.approval == "pending"]
    if not waiting:
        raise ValueError("nothing_pending")
    if not ro.public_token:
        ro.public_token = secrets.token_urlsafe(32)
    total = sum(li.total for li in waiting)
    summary = f"Job #{ro.id}: {len(waiting)} repair{'s' if len(waiting) != 1 else ''}, ${total:,.2f} in pending line items for review; verify tax treatment."
    now = datetime.now().astimezone()
    # Everything is worked out before the item joins the session, so no early save sees it half-filled.
    fields = dict(customer_phone=phone, customer_id=ro.customer_id, vehicle_id=ro.vehicle_id,
                  summary=summary, draft_reply=text or default_text(ro, offer_snap),
                  proposed_action=f"Text the approval link for job #{ro.id}",
                  # The send is refused later if the customer's thread has moved past this message.
                  last_message_id=newest_message_id or "none", thread_message_id=None, updated_at=now)
    item = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                         DispatchItem.status.in_(OPEN_STATUSES)).with_for_update().first()
    if item is None:
        item = DispatchItem(quo_conversation_id=conversation_key(ro.id), category=CATEGORY, urgency="normal",
                            status="needs_owner", created_at=now, **fields)
        db.add(item)
    else:
        for name, value in fields.items():
            setattr(item, name, value)
    db.flush()
    db.add(DispatchAudit(item_id=item.id, actor=actor[:64], action="approval_link_queued", detail=f"job {ro.id}"))
    return item


def close_for(db: Session, ro: RepairOrder, actor: str = "customer", action: str = "approval_link_answered") -> None:
    """The customer answered, so the link item is finished whether or not the owner sent it."""
    rows = db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                         DispatchItem.status.in_(OPEN_STATUSES + ("approved",))).all()
    for item in rows:
        item.status, item.updated_at = "closed", datetime.now().astimezone()
        db.add(DispatchAudit(item_id=item.id, actor=actor, action=action, detail=f"job {ro.id}"))


DASHBOARD_REPLY = "dashboard_reply"  # the owner-approved reply queue the messaging service sends from


def custom_text(ro: RepairOrder, text: str, offer_snap: bool = False) -> str:
    """The sender's own wording, always carrying this job's link exactly once."""
    text = str(text or "").replace("\r", "").strip()
    if ro.public_token not in text:
        text = f"{text}\n\nReview and approve here: {base_url()}/status/{ro.public_token}"
    return snap_offer.add(text, offer_snap)


def _already_texted(ro: RepairOrder, messages: list) -> bool:
    """True when a text after the customer's last message already carries this job's link,
    whoever sent it (the dashboard, Quo itself or Claude), so it is never texted twice."""
    token = ro.public_token or ""
    if not token:
        return False
    after = []
    for m in messages:
        if str(m.get("direction", "")).upper() == "INBOUND":
            after = []
        else:
            after.append(m)
    return any(token in str(m.get("body") or "") for m in after)


# A text handed to the shop line that Quo hasn't confirmed by now never went out.
NO_ANSWER_AFTER = timedelta(minutes=20)
NOT_SENT_WHY = {"failed": "the shop line couldn't send it",
                "skipped": "the shop line held it back (a newer text in their thread, or a safety check)",
                "expired": "the shop line never confirmed it went out"}


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _settle(db: Session, rows: list) -> None:
    """A text still waiting on the shop line long after it was handed over counts as not sent, so the
    owner can send it again; it is also taken out of the queue so it can't go out late as a double."""
    now = datetime.now(timezone.utc)
    for row in rows:
        started = _aware(row.updated_at or row.created_at)
        if row.status in ("queued", "reserved") and started is not None and now - started > NO_ANSWER_AFTER:
            row.status, row.updated_at = "failed", now
            db.add(DispatchAudit(item_id=row.item_id, actor="system", action="text_expired",
                                 detail="no word from the shop line that it went out"))


def _rows_for(db: Session, ro: RepairOrder) -> list:
    from app.dispatch_model import DispatchOutbound
    ids = [i for (i,) in db.query(DispatchItem.id).filter(
        DispatchItem.quo_conversation_id == conversation_key(ro.id)).all()]
    if not ids:
        return []
    return (db.query(DispatchOutbound).filter(DispatchOutbound.item_id.in_(ids))
            .order_by(DispatchOutbound.id.desc()).all())


def last_send(db: Session, ro: RepairOrder) -> dict | None:
    """What really happened to the newest approval text for this job, for the job page. Read only.

    state: sent (Quo took it), waiting (handed to the shop line, no answer yet) or not_sent.
    """
    rows = _rows_for(db, ro)
    if not rows:
        return None
    row = rows[0]
    at = _aware(row.updated_at or row.created_at)
    if row.status == "sent":
        return {"state": "sent", "at": at, "item_id": row.item_id}
    if row.status in ("queued", "reserved"):
        if at is not None and datetime.now(timezone.utc) - at > NO_ANSWER_AFTER:
            return {"state": "not_sent", "at": at, "item_id": row.item_id, "why": NOT_SENT_WHY["expired"]}
        return {"state": "waiting", "at": at, "item_id": row.item_id}
    expired = row.status == "failed" and db.query(DispatchAudit.id).filter(
        DispatchAudit.item_id == row.item_id, DispatchAudit.action == "text_expired").first() is not None
    why = "expired" if expired else row.status
    return {"state": "not_sent", "at": at, "item_id": row.item_id, "why": NOT_SENT_WHY.get(why, NOT_SENT_WHY["failed"])}


def sign_link(ro: RepairOrder) -> str | None:
    """The page where the customer approves, for the owner to copy and send another way."""
    return f"{base_url()}/sign/ro/{ro.public_token}" if ro.public_token else None


def send_now(db: Session, ro: RepairOrder, actor: str, actor_user_id: int, offer_snap: bool = False,
             text: str | None = None, audit_detail: str | None = None,
             resend: bool = False) -> tuple[str, DispatchItem | None]:
    """Texts the customer their approval link now. Returns (state, item).

    state is one of: paused, no_phone, nothing, unavailable, already, texted (the link went out
    outside the dashboard and they haven't replied), in_flight (an earlier press is still with the
    shop line), sending (handed to the shop line).
    "already" counts only a text Quo confirmed (or one still in flight): a send the shop line failed,
    held back or never confirmed can be sent again. resend=True is the owner's "Send it again": it
    skips the already-sent checks, but never doubles a text that is still in flight.
    Undecided lines are marked waiting on the customer whatever happens, as Ask for approval did.
    When lines changed or were added since the customer last answered (app/line_changes.py), the
    text says the estimate was updated and is sent once per set of changes, even though their
    earlier link already went out. Lines they approved and nobody changed stay approved.
    """
    from app import customer_file, website_alerts
    from app.dispatch_model import DispatchOutbound
    from app.sending_pause import is_paused

    from app import line_changes

    if is_paused(db):
        return "paused", None
    phone = mobile(ro.customer.phone)
    if phone is None:
        return "no_phone", None
    # Lines changed or added since the customer last answered: text "we updated your estimate", once
    # per set of changes, even though their first link already went out.
    changes = line_changes.has_changes(ro)
    fresh = [li for li in ro.line_items if li.approval is None]
    for li in fresh:
        li.approval = "pending"
    if changes:
        if ro.status in line_changes.REASK_FROM:
            ro.status = "waiting_approval"
        website_alerts.close_for_job(db, ro, actor, "estimate changes texted")
    elif fresh:
        ro.status = "waiting_approval"
        website_alerts.close_for_job(db, ro, actor, "approval link texted")
    if not any(li.approval == "pending" for li in ro.line_items):
        db.commit()
        return "nothing", None
    if not ro.public_token:
        ro.public_token = secrets.token_urlsafe(32)
    db.commit()
    thread, problem = customer_file._bridge("/conversation", phone[-10:])
    if problem:
        return "unavailable", None
    messages = thread.get("messages") or []
    newest = (messages[-1].get("id") if messages else None) or "none"
    earlier = _rows_for(db, ro)
    _settle(db, earlier)
    db.commit()
    if any(o.status in ("queued", "reserved") for o in earlier):
        return "in_flight", None
    if resend:
        prefix = f"resend:{ro.id}:{newest}"[:150]
    elif changes:
        # One send per set of changes. Their earlier link text doesn't count: this is new.
        prefix = f"changes:{ro.id}:{line_changes.revision_key(ro)}"
    else:
        sent_items = {o.item_id for o in earlier if o.status == "sent"}
        already = (db.query(DispatchItem).filter(DispatchItem.quo_conversation_id == conversation_key(ro.id),
                                                 DispatchItem.status == "approved",
                                                 DispatchItem.last_message_id == newest,
                                                 DispatchItem.id.in_(sent_items or {-1})).first())
        if already is not None:
            # Quo confirmed this link and nothing has happened in their thread since: never text it twice.
            return "already", already
        if _already_texted(ro, messages):
            return "texted", None
        # One send per job per point in their thread. A failed or skipped try may be tried again.
        prefix = f"approval:{ro.id}:{newest}"[:150]
    tries = db.query(DispatchOutbound).filter(DispatchOutbound.dedupe_key.like(prefix + "%")).all()
    if not resend and any(o.status not in ("failed", "skipped") for o in tries):
        return "already", None
    body = custom_text(ro, text, offer_snap) if text else (line_changes.message(ro, offer_snap) if changes else None)
    item = queue(db, ro, newest, actor, offer_snap=offer_snap, text=body)
    now = datetime.now().astimezone()
    item.status, item.decided_by, item.decided_at, item.updated_at = "approved", actor_user_id, now, now
    db.add(DispatchAudit(item_id=item.id, actor=actor[:64], action="owner_approve",
                         detail=audit_detail or f"text link now, job {ro.id}"))
    db.add(DispatchOutbound(dedupe_key=f"{prefix}:{len(tries)}" if tries else prefix, kind=DASHBOARD_REPLY,
                            item_id=item.id, status="queued"))
    try:
        db.commit()
    except IntegrityError:
        # Two presses at once: the other one is already sending it.
        db.rollback()
        return "already", None
    return "sending", item
