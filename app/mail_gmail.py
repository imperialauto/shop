"""Talks to Gmail for the dashboard inbox: sign-in, the stored token, and the calls the inbox makes.

One Internal Google app in the shop's Workspace. Scopes are read/label/archive (gmail.modify) and
send (gmail.send); nothing here can delete mail. The refresh token is kept encrypted with
MAIL_TOKEN_KEY (a Fernet key set as a Railway variable) and is never logged or shown.

Env:
  GOOGLE_MAIL_CLIENT_ID, GOOGLE_MAIL_CLIENT_SECRET  the Internal OAuth app
  MAIL_TOKEN_KEY                                    Fernet key for the stored token
  MAIL_OAUTH_REDIRECT_URI                           optional; otherwise built from the request
"""
import base64
import os
from urllib.parse import urlencode

SCOPES = ("https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send")
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class MailError(Exception):
    """A Gmail call failed. The message is safe to show the owner (no tokens, no mail text)."""


class HistoryGone(MailError):
    """The stored history point is too old; the inbox has to resync."""


def client_id() -> str:
    return os.getenv("GOOGLE_MAIL_CLIENT_ID", "").strip()


def client_secret() -> str:
    return os.getenv("GOOGLE_MAIL_CLIENT_SECRET", "").strip()


def setup_missing() -> list[str]:
    """Railway variables still needed before the mailbox can be connected."""
    missing = [name for name in ("GOOGLE_MAIL_CLIENT_ID", "GOOGLE_MAIL_CLIENT_SECRET", "MAIL_TOKEN_KEY")
               if not os.getenv(name, "").strip()]
    if "MAIL_TOKEN_KEY" not in missing:
        try:
            _fernet()
        except MailError:
            missing.append("MAIL_TOKEN_KEY (not a valid key)")
    return missing


def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv("MAIL_TOKEN_KEY", "").strip()
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        raise MailError("MAIL_TOKEN_KEY is missing or not a valid key.")


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt_token(blob: str) -> str:
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt((blob or "").encode()).decode()
    except InvalidToken:
        raise MailError("The saved mailbox sign-in can't be read. Connect the mailbox again.")


def auth_url(state: str, redirect_uri: str, login_hint: str = "") -> str:
    params = {"client_id": client_id(), "redirect_uri": redirect_uri, "response_type": "code",
              "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state}
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str, post=None) -> str:
    """Trades the one-time sign-in code for a refresh token. Raises MailError on anything short of that."""
    import httpx
    post = post or httpx.post
    try:
        resp = post(TOKEN_URL, data={"code": code, "client_id": client_id(), "client_secret": client_secret(),
                                     "redirect_uri": redirect_uri, "grant_type": "authorization_code"}, timeout=15)
    except httpx.HTTPError:
        raise MailError("Google didn't answer. Try connecting again.")
    if resp.status_code != 200:
        raise MailError("Google turned the sign-in down. Try connecting again.")
    data = resp.json()
    granted = set((data.get("scope") or "").split())
    if not set(SCOPES) <= granted:
        raise MailError("Reading and sending mail both have to be allowed. Connect again and tick both.")
    if not data.get("refresh_token"):
        raise MailError("Google didn't give a lasting sign-in. Connect again.")
    return data["refresh_token"]


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4))


class GmailClient:
    """The few Gmail calls the inbox uses. Tests swap in a fake with the same methods."""

    def __init__(self, refresh_token: str):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials(token=None, refresh_token=refresh_token, token_uri=TOKEN_URL, client_id=client_id(),
                            client_secret=client_secret(), scopes=list(SCOPES))
        self._svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self._users = self._svc.users()

    def _run(self, request):
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError
        try:
            return request.execute(num_retries=2)
        except RefreshError:
            raise MailError("The mailbox sign-in expired or was removed. Connect the mailbox again.")
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status == 404:
                raise HistoryGone("Gmail no longer has that history point.")
            if status == 429:
                raise MailError("Gmail asked us to slow down. It will catch up on the next check.")
            raise MailError(f"Gmail said no ({status}).")
        except OSError:
            raise MailError("Gmail couldn't be reached.")

    def profile(self) -> dict:
        return self._run(self._users.getProfile(userId="me"))

    def list_ids(self, query: str, limit: int) -> list[str]:
        ids, token = [], None
        while len(ids) < limit:
            page = self._run(self._users.messages().list(userId="me", q=query, maxResults=min(100, limit - len(ids)),
                                                         pageToken=token))
            ids += [m["id"] for m in page.get("messages", [])]
            token = page.get("nextPageToken")
            if not token:
                break
        return ids

    def history(self, start_id: str) -> tuple[list[dict], str]:
        records, token, newest = [], None, start_id
        while True:
            page = self._run(self._users.history().list(userId="me", startHistoryId=start_id, pageToken=token,
                                                        historyTypes=["messageAdded", "labelAdded", "labelRemoved"]))
            records += page.get("history", [])
            newest = page.get("historyId", newest)
            token = page.get("nextPageToken")
            if not token:
                return records, str(newest)

    def get_message(self, message_id: str) -> dict:
        return self._run(self._users.messages().get(userId="me", id=message_id, format="full"))

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        part = self._run(self._users.messages().attachments().get(userId="me", messageId=message_id, id=attachment_id))
        return _b64(part.get("data", ""))

    def modify_thread(self, thread_id: str, add=(), remove=()) -> None:
        self._run(self._users.threads().modify(userId="me", id=thread_id,
                                               body={"addLabelIds": list(add), "removeLabelIds": list(remove)}))

    def send(self, raw: bytes, thread_id: str | None = None) -> dict:
        body = {"raw": base64.urlsafe_b64encode(raw).decode()}
        if thread_id:
            body["threadId"] = thread_id
        return self._run(self._users.messages().send(userId="me", body=body))


# Tests set this to return a fake client.
_factory = None


def client_from_token(refresh_token: str) -> "GmailClient":
    if _factory is not None:
        return _factory(None)
    return GmailClient(refresh_token)


def client_for(account) -> "GmailClient":
    if _factory is not None:
        return _factory(account)
    if not account or not account.token_encrypted:
        raise MailError("No mailbox is connected.")
    return GmailClient(decrypt_token(account.token_encrypted))
