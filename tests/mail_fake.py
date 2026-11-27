"""A pretend Gmail mailbox for the Email page's tests and local previews. Made-up data only."""
import base64
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

from app import mail_gmail


def _b64(text) -> str:
    data = text if isinstance(text, bytes) else text.encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def raw_message(mid, thread, sender, subject, text, to="shop@example-auto.test", html=None, labels=("INBOX", "UNREAD"),
                when=None, reply_to="", attachments=(), message_id=None):
    when = when or datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Date", "value": format_datetime(when)},
               {"name": "Message-ID", "value": message_id or f"<{mid}@mail.example.test>"}]
    if reply_to:
        headers.append({"name": "Reply-To", "value": reply_to})
    parts = [{"mimeType": "text/plain", "filename": "", "body": {"data": _b64(text), "size": len(text)},
              "headers": [{"name": "Content-Type", "value": "text/plain; charset=UTF-8"}]}]
    if html:
        parts.append({"mimeType": "text/html", "filename": "", "body": {"data": _b64(html), "size": len(html)}})
    body = {"mimeType": "multipart/alternative", "parts": parts}
    if attachments:
        body = {"mimeType": "multipart/mixed", "parts": [body] + [
            {"mimeType": mime, "filename": name, "body": {"attachmentId": f"att-{mid}-{i}", "size": len(data)}}
            for i, (name, mime, data) in enumerate(attachments)]}
    body["headers"] = headers
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "snippet": text[:80],
            "internalDate": str(int(when.timestamp() * 1000)), "payload": body,
            "_files": {f"att-{mid}-{i}": data for i, (_, _, data) in enumerate(attachments)}}


class FakeGmail:
    def __init__(self, address="shop@example-auto.test"):
        self.address = address
        self.messages = {}
        self.history_id = 100
        self.records = []
        self.sent = []
        self.modified = []
        self.fail = None  # set to an exception to make the next call fail
        self.history_gone = False

    # mailbox setup
    def add(self, raw, record=True):
        self.messages[raw["id"]] = raw
        self.history_id += 1
        if record:
            self.records.append({"id": str(self.history_id), "messagesAdded": [
                {"message": {"id": raw["id"], "threadId": raw["threadId"], "labelIds": raw["labelIds"]}}]})
        return raw

    def relabel(self, mid, labels):
        self.messages[mid]["labelIds"] = list(labels)
        self.history_id += 1
        self.records.append({"id": str(self.history_id), "labelsAdded": [
            {"message": {"id": mid, "threadId": self.messages[mid]["threadId"], "labelIds": list(labels)}}]})

    def _check(self):
        if self.fail:
            exc, self.fail = self.fail, None
            raise exc

    # the client interface
    def profile(self):
        self._check()
        return {"emailAddress": self.address, "historyId": str(self.history_id)}

    def list_ids(self, query, limit):
        self._check()
        return [m for m, raw in sorted(self.messages.items(), key=lambda kv: kv[1]["internalDate"], reverse=True)
                if not set(raw["labelIds"]) & {"SPAM", "TRASH", "DRAFT"}][:limit]

    def history(self, start_id):
        self._check()
        if self.history_gone:
            raise mail_gmail.HistoryGone("gone")
        recs = [r for r in self.records if int(r["id"]) > int(start_id)]
        return recs, str(self.history_id)

    def get_message(self, mid):
        self._check()
        if mid not in self.messages:
            raise mail_gmail.HistoryGone("missing")
        raw = dict(self.messages[mid])
        raw.pop("_files", None)
        return raw

    def get_attachment(self, mid, att_id):
        self._check()
        return self.messages[mid]["_files"][att_id]

    def modify_thread(self, thread_id, add=(), remove=()):
        self._check()
        self.modified.append((thread_id, tuple(add), tuple(remove)))
        for raw in self.messages.values():
            if raw["threadId"] == thread_id:
                labels = (set(raw["labelIds"]) | set(add)) - set(remove)
                raw["labelIds"] = sorted(labels)

    def _after(self):
        newest = max([int(r["internalDate"]) for r in self.messages.values()] or [0]) / 1000
        return max(datetime.now(timezone.utc), datetime.fromtimestamp(newest, tz=timezone.utc)) + timedelta(seconds=1)

    def send(self, raw_bytes, thread_id=None):
        self._check()
        from email import message_from_bytes, policy
        parsed = message_from_bytes(raw_bytes, policy=policy.default)
        mid = f"sent{len(self.sent) + 1}"
        thread = thread_id or f"t-{mid}"
        body = parsed.get_body(("plain",)).get_content()
        self.sent.append({"to": str(parsed["To"]), "cc": parsed["Cc"], "subject": str(parsed["Subject"]), "body": body,
                          "in_reply_to": str(parsed["In-Reply-To"] or "") or None, "thread": thread_id, "raw": raw_bytes,
                          "files": [p.get_filename() for p in parsed.iter_attachments()]})
        self.add(raw_message(mid, thread, f"Shop <{self.address}>", str(parsed["Subject"]), body, to=str(parsed["To"]),
                             labels=("SENT",), when=self._after()))
        return {"id": mid, "threadId": thread}


def install(fake):
    mail_gmail._factory = lambda account: fake


def uninstall():
    mail_gmail._factory = None
