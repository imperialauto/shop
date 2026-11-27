"""Email inside the dashboard: the connected mailbox and a local copy of its mail.

Gmail stays the record. These tables are a copy for fast lists, search, sorting and links to
customers and jobs; a resync rebuilds them. Production migration: migrations/20261127_mail_inbox.sql
(not auto-applied). The tables are only checked at startup when MAIL_INBOX_ENABLED=true.
"""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text

from app.database import Base

TABLES = ("mail_accounts", "mail_threads", "mail_messages", "mail_attachments")

# Tab slug -> label, in the order the inbox shows them.
CATEGORIES = {
    "leads": "Leads & customers",
    "uhaul": "U-Haul",
    "bills": "Bills",
    "vendors": "Parts & vendors",
    "other": "Other",
}


def enabled() -> bool:
    return os.getenv("MAIL_INBOX_ENABLED", "").strip().lower() == "true"


def utc_now():
    return datetime.now(timezone.utc)


class MailAccount(Base):
    """One connected mailbox. The refresh token is stored encrypted (see app.mail_gmail)."""
    __tablename__ = "mail_accounts"
    id = Column(Integer, primary_key=True)
    address = Column(String(255), nullable=False, unique=True)
    provider = Column(String(16), nullable=False, default="gmail")
    token_encrypted = Column(Text)
    history_id = Column(String(32))  # Gmail history point the last sync reached
    status = Column(String(16), nullable=False, default="connected")  # connected, error, disconnected
    last_sync_at = Column(DateTime(timezone=True))
    last_error = Column(String(255))
    connected_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), default=utc_now)


class MailThread(Base):
    __tablename__ = "mail_threads"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("mail_accounts.id"), nullable=False)
    gmail_thread_id = Column(String(64), nullable=False)
    subject = Column(String(512), nullable=False, default="")
    last_from = Column(String(255), nullable=False, default="")  # display name or address of the newest sender
    last_at = Column(DateTime(timezone=True))
    snippet = Column(Text, nullable=False, default="")
    message_count = Column(Integer, nullable=False, default=0)
    unread = Column(Boolean, nullable=False, default=False)
    in_inbox = Column(Boolean, nullable=False, default=True)
    starred = Column(Boolean, nullable=False, default=False)
    category = Column(String(16), nullable=False, default="other")
    category_locked = Column(Boolean, nullable=False, default=False)  # the owner moved it; sorting leaves it
    urgent = Column(Boolean, nullable=False, default=False)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id"))
    updated_at = Column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("uq_mail_threads_account_thread", "account_id", "gmail_thread_id", unique=True),
        Index("ix_mail_threads_list", "account_id", "in_inbox", "category", "last_at"),
    )


class MailMessage(Base):
    __tablename__ = "mail_messages"
    id = Column(Integer, primary_key=True)
    thread_id = Column(Integer, ForeignKey("mail_threads.id"), nullable=False, index=True)
    gmail_message_id = Column(String(64), nullable=False, unique=True)
    rfc_message_id = Column(String(512))  # the Message-ID header, for replies that thread correctly
    from_name = Column(String(255), nullable=False, default="")
    from_address = Column(String(255), nullable=False, default="")
    reply_to = Column(String(512), nullable=False, default="")
    to_addresses = Column(Text, nullable=False, default="")
    cc_addresses = Column(Text, nullable=False, default="")
    subject = Column(String(512), nullable=False, default="")
    sent_at = Column(DateTime(timezone=True))
    snippet = Column(Text, nullable=False, default="")
    body_text = Column(Text, nullable=False, default="")
    body_html = Column(Text)
    labels = Column(Text, nullable=False, default="[]")  # Gmail label ids, JSON list
    outgoing = Column(Boolean, nullable=False, default=False)  # sent from the connected mailbox
    created_at = Column(DateTime(timezone=True), default=utc_now)

    def label_set(self) -> set:
        try:
            return set(json.loads(self.labels or "[]"))
        except ValueError:
            return set()


class MailAttachment(Base):
    """What a message carries. The file itself stays in Gmail and is fetched when opened."""
    __tablename__ = "mail_attachments"
    id = Column(Integer, primary_key=True)
    message_id = Column(Integer, ForeignKey("mail_messages.id"), nullable=False, index=True)
    gmail_attachment_id = Column(Text, nullable=False)
    filename = Column(String(255), nullable=False, default="attachment")
    mime_type = Column(String(128), nullable=False, default="application/octet-stream")
    size = Column(Integer, nullable=False, default=0)
