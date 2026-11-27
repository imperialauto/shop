"""Turns a Gmail message into what the inbox stores, and builds outgoing replies.

Mail text is shown as text. The HTML version is only ever shown inside a sandboxed frame with
scripts off (see routes/mail.py), so nothing here has to make HTML safe.
"""
import base64
import html
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4))


def _decode(part: dict) -> str:
    raw = _b64((part.get("body") or {}).get("data", ""))
    charset = "utf-8"
    for h in part.get("headers") or []:
        if h.get("name", "").lower() == "content-type":
            m = re.search(r'charset="?([\w\-]+)', h.get("value", ""), re.I)
            if m:
                charset = m.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.href = [], 0, None

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag == "a" and self.href:
            if self.href.startswith(("http://", "https://")) and self.href not in "".join(self.out[-3:]):
                self.out.append(f" ({self.href})")
            self.href = None

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    try:
        parser.feed(markup or "")
        parser.close()
    except Exception:  # malformed mail; fall back to stripping tags
        return re.sub(r"<[^>]+>", " ", html.unescape(markup or ""))
    text = "".join(parser.out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def _walk(part: dict, found: dict):
    mime = (part.get("mimeType") or "").lower()
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename and body.get("attachmentId"):
        found["attachments"].append({"gmail_attachment_id": body["attachmentId"], "filename": filename[:255],
                                     "mime_type": mime[:128] or "application/octet-stream",
                                     "size": int(body.get("size") or 0)})
        return
    if mime == "text/plain" and not filename and "text" not in found:
        found["text"] = _decode(part)
    elif mime == "text/html" and not filename and "html" not in found:
        found["html"] = _decode(part)
    for child in part.get("parts") or []:
        _walk(child, found)


def header(headers: list, name: str) -> str:
    name = name.lower()
    return next((h.get("value", "") for h in headers or [] if h.get("name", "").lower() == name), "")


def parse_message(msg: dict) -> dict:
    """Gmail format=full message -> fields for MailMessage, plus 'attachments' and 'thread' ids."""
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    found = {"attachments": []}
    _walk(payload, found)
    body_html = found.get("html")
    body_text = found.get("text") or (html_to_text(body_html) if body_html else "")
    from_name, from_address = parseaddr(header(headers, "From"))
    sent_at = None
    if msg.get("internalDate"):  # when Gmail got it; a sender's Date header can be wrong
        sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    else:
        try:
            sent_at = parsedate_to_datetime(header(headers, "Date"))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return {
        "gmail_message_id": msg["id"], "gmail_thread_id": msg.get("threadId") or msg["id"],
        "rfc_message_id": header(headers, "Message-ID")[:512] or None,
        "references": header(headers, "References"),
        "from_name": (from_name or "")[:255], "from_address": (from_address or "").lower()[:255],
        "reply_to": header(headers, "Reply-To")[:512], "to_addresses": header(headers, "To"),
        "cc_addresses": header(headers, "Cc"), "subject": header(headers, "Subject")[:512],
        "list_unsubscribe": header(headers, "List-Unsubscribe"),
        "sent_at": sent_at, "snippet": html.unescape(msg.get("snippet") or "")[:500],
        "body_text": body_text, "body_html": body_html, "labels": list(msg.get("labelIds") or []),
        "attachments": found["attachments"],
    }


NOREPLY = re.compile(r"(^|[._\-])(no-?reply|do-?not-?reply|donotreply|mailer-daemon|notifications?)([._\-@]|$)", re.I)


def is_noreply(address: str) -> bool:
    return bool(NOREPLY.search((address or "").split("@")[0] + "@"))


def addresses(value: str) -> list[str]:
    return [a.lower() for _, a in getaddresses([value or ""]) if "@" in a]


def reply_target(message) -> str:
    """Who a reply goes to: the Reply-To the sender asked for, else the sender."""
    targets = addresses(message.reply_to) or [message.from_address]
    return ", ".join(t for t in targets if t)


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if re.match(r"^re:", subject, re.I) else f"Re: {subject}".strip()


def build_message(sender: str, sender_name: str, to: str, subject: str, body: str, cc: str = "",
                  in_reply_to: str | None = None, references: str = "", files=()) -> bytes:
    """An outgoing message as bytes for Gmail's send call. files is [(filename, mime, bytes)]."""
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender)) if sender_name else sender
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] or None))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = f"{references} {in_reply_to}".strip()
    msg.set_content(body)
    for name, mime, data in files:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=name)
    return msg.as_bytes()
