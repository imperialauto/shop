"""Pulls the owner's bank balances and transactions in through Plaid.

Setup is three Railway variables: PLAID_CLIENT_ID, PLAID_SECRET and, optionally, PLAID_ENV
(production by default; sandbox for testing) and PLAID_REDIRECT_URI (needed for some banks on a
phone). Until the keys are set the Money page shows a short note and nothing here runs.

What a pull does, for each linked bank:
  1. reads new, changed and removed transactions since the last pull (Plaid's /transactions/sync),
  2. reads each account's balance,
  3. sets the balance of the Money page account each cash account is tied to, and
  4. marks an open bill paid when a new posted payment out of an account of the same kind
     (shop or personal) matches its exact amount within ten days of its due date.
It never moves money: the Plaid key only reads.
"""
import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app import finance_plan as plan
from app.bank_model import BankAccount, BankLink, BankTransaction
from app.config import SECRET_KEY
from app.finance_model import FinanceAccount, FinanceBill, FinanceChange

ENVIRONMENTS = {"sandbox": "https://sandbox.plaid.com", "production": "https://production.plaid.com"}
CASH_KINDS = ("depository",)
ACTOR = "bank feed"
TIMEOUT = 20.0
MAX_PAGES = 20  # 500 transactions a page; a first pull of 90 days fits easily


class PlaidError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(code)
        self.code = code or "UNKNOWN"
        self.message = message


def settings() -> dict:
    env = os.getenv("PLAID_ENV", "production").strip().lower()
    return {"client_id": os.getenv("PLAID_CLIENT_ID", "").strip(),
            "secret": os.getenv("PLAID_SECRET", "").strip(),
            "env": env if env in ENVIRONMENTS else "production",
            "redirect_uri": os.getenv("PLAID_REDIRECT_URI", "").strip()}


def enabled() -> bool:
    s = settings()
    return bool(s["client_id"] and s["secret"])


# ---- access tokens are kept encrypted --------------------------------------------

def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"bank-feed:{SECRET_KEY}".encode()).digest()))


def seal(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def unseal(sealed: str) -> str | None:
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except (InvalidToken, ValueError):
        return None


# ---- Plaid calls ------------------------------------------------------------------

class PlaidClient:
    """The few Plaid endpoints the feed needs. Errors become PlaidError with Plaid's error code;
    Plaid's message text and the keys never reach logs or the page."""

    def __init__(self, http: httpx.Client | None = None):
        s = settings()
        self.client_id, self.secret, self.redirect_uri = s["client_id"], s["secret"], s["redirect_uri"]
        self.base = ENVIRONMENTS[s["env"]]
        self.http = http or httpx.Client(timeout=TIMEOUT)

    def call(self, path: str, body: dict) -> dict:
        payload = {"client_id": self.client_id, "secret": self.secret, **body}
        try:
            response = self.http.post(f"{self.base}{path}", json=payload)
        except httpx.HTTPError as exc:
            raise PlaidError("NETWORK", type(exc).__name__) from None
        try:
            data = response.json()
        except ValueError:
            raise PlaidError(f"HTTP_{response.status_code}") from None
        if response.status_code >= 400 or "error_code" in data and data.get("error_code"):
            raise PlaidError(str(data.get("error_code") or f"HTTP_{response.status_code}"),
                             str(data.get("display_message") or "")[:200])
        return data

    def link_token(self, user_id: int, access_token: str | None = None) -> str:
        body = {"client_name": "Imperial Operations", "user": {"client_user_id": f"owner-{user_id}"},
                "country_codes": ["US"], "language": "en"}
        if access_token:
            body["access_token"] = access_token  # update mode: the owner signs in to the bank again
        else:
            body["products"] = ["transactions"]
            body["transactions"] = {"days_requested": 90}
        if self.redirect_uri:
            body["redirect_uri"] = self.redirect_uri
        return self.call("/link/token/create", body)["link_token"]

    def exchange(self, public_token: str) -> tuple[str, str]:
        data = self.call("/item/public_token/exchange", {"public_token": public_token})
        return data["access_token"], data["item_id"]

    def accounts(self, access_token: str) -> list[dict]:
        return self.call("/accounts/get", {"access_token": access_token}).get("accounts", [])

    def transactions_sync(self, access_token: str, cursor: str | None) -> dict:
        body = {"access_token": access_token, "count": 500}
        if cursor:
            body["cursor"] = cursor
        return self.call("/transactions/sync", body)

    def remove(self, access_token: str) -> None:
        self.call("/item/remove", {"access_token": access_token})


# ---- turning Plaid data into ours ----------------------------------------------------

def to_cents(value) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def short_error(exc: PlaidError) -> tuple[str, str]:
    """The link status and a plain-words note for the owner."""
    if exc.code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION", "PENDING_DISCONNECT", "ACCESS_NOT_GRANTED"):
        return "needs_login", "The bank wants you to sign in again. Tap Reconnect."
    if exc.code == "NETWORK":
        return "error", "Couldn't reach Plaid. It will try again at the next pull."
    if exc.code in ("INVALID_API_KEYS", "INVALID_ACCESS_TOKEN", "UNAUTHORIZED_ENVIRONMENT"):
        return "error", "Plaid turned down the keys. Check PLAID_CLIENT_ID, PLAID_SECRET and PLAID_ENV in Railway."
    if exc.code in ("INSTITUTION_DOWN", "INSTITUTION_NOT_RESPONDING", "INTERNAL_SERVER_ERROR", "PRODUCT_NOT_READY"):
        return "error", "The bank didn't answer this time. It will try again at the next pull."
    return "error", f"Plaid said {exc.code}. It will try again at the next pull."


def account_label(institution: str, name: str, mask: str | None) -> str:
    label = f"{institution} {name}".strip()
    if mask:
        label = f"{label} ...{mask}"
    return label[:80]


def _log(db: Session, action: str, detail: str):
    db.add(FinanceChange(actor=ACTOR, action=action, detail=detail[:2000]))


def save_accounts(db: Session, link: BankLink, rows: list[dict], today: date) -> list[BankAccount]:
    """Adds or refreshes the link's accounts. A new cash account gets its own Money page account
    (shop by default); a credit card is shown with what's owed but never counted as cash."""
    saved = []
    for row in rows:
        plaid_id = str(row.get("account_id") or "")
        if not plaid_id:
            continue
        balances = row.get("balances") or {}
        account = db.query(BankAccount).filter(BankAccount.plaid_account_id == plaid_id).first()
        if account is None:
            account = BankAccount(link_id=link.id, plaid_account_id=plaid_id, scope="business", tracked=True)
            db.add(account)
        account.name = str(row.get("name") or row.get("official_name") or "Account")[:120]
        account.mask = (str(row.get("mask")) if row.get("mask") else None)
        account.kind = str(row.get("type") or "depository")[:24]
        account.subtype = (str(row.get("subtype"))[:32] if row.get("subtype") else None)
        account.current_cents = to_cents(balances.get("current"))
        account.available_cents = to_cents(balances.get("available"))
        account.updated_at = datetime.now(timezone.utc)
        db.flush()
        if account.kind in CASH_KINDS and account.tracked:
            feed_balance(db, account, link, today)
        saved.append(account)
    return saved


def spendable_cents(account: BankAccount) -> int | None:
    """What can be spent today: the bank's available figure, or the current balance if it gives none."""
    return account.available_cents if account.available_cents is not None else account.current_cents


def feed_balance(db: Session, account: BankAccount, link: BankLink, today: date):
    balance = spendable_cents(account)
    if balance is None:
        return
    target = db.get(FinanceAccount, account.finance_account_id) if account.finance_account_id else None
    if target is None:
        label = account_label(link.institution, account.name, account.mask)
        target = db.query(FinanceAccount).filter(FinanceAccount.name == label).first()
        if target is None:
            target = FinanceAccount(name=label, scope=account.scope, balance_cents=0, held_cents=0, as_of=today, active=True)
            db.add(target)
            db.flush()
        account.finance_account_id = target.id
    if target.balance_cents != balance or target.as_of != today:
        target.balance_cents, target.as_of = balance, today
        target.updated_at = datetime.now(timezone.utc)
        _log(db, "bank_balance", f"{target.name} {plan.dollars(balance)}")
    target.active = True


def apply_transactions(db: Session, page: dict) -> list[BankTransaction]:
    """Saves one /transactions/sync page and returns the transactions that are new to us."""
    by_plaid = {a.plaid_account_id: a for a in db.query(BankAccount).all()}
    new = []
    for row in list(page.get("added") or []) + list(page.get("modified") or []):
        account = by_plaid.get(str(row.get("account_id") or ""))
        tid = str(row.get("transaction_id") or "")
        if account is None or not tid:
            continue
        try:
            day = date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        txn = db.query(BankTransaction).filter(BankTransaction.plaid_transaction_id == tid).first()
        if txn is None:
            txn = BankTransaction(plaid_transaction_id=tid)
            db.add(txn)
            new.append(txn)
        category = (row.get("personal_finance_category") or {}).get("primary")
        txn.bank_account_id, txn.day = account.id, day
        txn.name = str(row.get("merchant_name") or row.get("name") or "")[:160] or "Transaction"
        txn.amount_cents = -to_cents(row.get("amount") or 0)  # Plaid counts money out as positive
        txn.pending = bool(row.get("pending"))
        txn.category = (str(category)[:60] if category else None)
    removed = [str(r.get("transaction_id")) for r in (page.get("removed") or []) if r.get("transaction_id")]
    if removed:
        db.query(BankTransaction).filter(BankTransaction.plaid_transaction_id.in_(removed)).delete(synchronize_session=False)
    db.flush()
    return new


def mark_bills_paid(db: Session, new_txns: list[BankTransaction], today: date) -> list[str]:
    """Open bills paid by a new, posted payment out of an account of the same kind. One payment
    pays one bill. Returns a note for each bill marked."""
    accounts = {a.id: a for a in db.query(BankAccount).all()}
    used, notes = set(), []
    open_bills = (db.query(FinanceBill).filter(FinanceBill.status == "open")
                  .order_by(FinanceBill.due_date, FinanceBill.id).all())
    for bill in open_bills:
        for txn in new_txns:
            account = accounts.get(txn.bank_account_id)
            if (txn.id in used or txn.pending or account is None or not account.tracked
                    or account.scope != bill.scope or txn.amount_cents != -bill.amount_cents
                    or abs((txn.day - bill.due_date).days) > 10):
                continue
            used.add(txn.id)
            bill.status, bill.paid_on = "paid", txn.day
            following = plan.next_due(bill.due_date, bill.repeats)
            if following is not None and not (db.query(FinanceBill).filter(
                    FinanceBill.name == bill.name, FinanceBill.due_date == following, FinanceBill.status == "open").first()):
                db.add(FinanceBill(name=bill.name, scope=bill.scope, category=bill.category, amount_cents=bill.amount_cents,
                                   due_date=following, repeats=bill.repeats, note=bill.note))
            _log(db, "bill_paid", f"{bill.name} {plan.dollars(bill.amount_cents)} due {bill.due_date}: matched {txn.name} on {txn.day}")
            notes.append(f"{bill.name} marked paid ({txn.name}, {txn.day.strftime('%b %-d')}).")
            break
    db.flush()
    return notes


@dataclass
class PullResult:
    banks: int = 0
    new_transactions: int = 0
    bills_paid: list = None
    problems: list = None


def pull_link(db: Session, client: PlaidClient, link: BankLink, today: date) -> tuple[list[BankTransaction], str | None]:
    """One bank: transactions since the last pull, then balances. Commits nothing; the caller does."""
    token = unseal(link.access_token_enc)
    if not token:
        link.status, link.last_error = "error", "The saved bank link can't be read. Remove it and connect the bank again."
        return [], link.last_error
    try:
        rows = client.accounts(token)
        save_accounts(db, link, rows, today)
        new, cursor = [], link.cursor
        for _ in range(MAX_PAGES):
            try:
                page = client.transactions_sync(token, cursor)
            except PlaidError as exc:
                if exc.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                    cursor, new = link.cursor, []  # Plaid asks to start this pull over
                    continue
                raise
            new += apply_transactions(db, page)
            cursor = page.get("next_cursor") or cursor
            if not page.get("has_more"):
                break
        link.cursor = cursor
    except PlaidError as exc:
        link.status, link.last_error = short_error(exc)
        return [], link.last_error
    link.status, link.last_error = "ok", None
    link.last_synced_at = datetime.now(timezone.utc)
    return new, None


def pull_all(db: Session, client: PlaidClient | None = None, today: date | None = None) -> PullResult:
    """Pulls every linked bank and commits once per bank, so one bank's trouble doesn't undo another's."""
    from app.routes.finance import phoenix_today
    today = today or phoenix_today()
    client = client or PlaidClient()
    result = PullResult(bills_paid=[], problems=[])
    for link in db.query(BankLink).order_by(BankLink.id).all():
        new, problem = pull_link(db, client, link, today)
        if problem:
            result.problems.append(f"{link.institution}: {problem}")
        else:
            result.banks += 1
            result.new_transactions += len(new)
            result.bills_paid += mark_bills_paid(db, new, today)
        db.commit()
    return result


def connect(db: Session, client: PlaidClient, public_token: str, institution: str, today: date) -> BankLink:
    """Finishes a Connect from Plaid's window: keeps the bank's token and pulls right away."""
    access_token, item_id = client.exchange(public_token)
    link = db.query(BankLink).filter(BankLink.item_id == item_id).first()
    if link is None:
        link = BankLink(item_id=item_id)
        db.add(link)
    link.institution = (institution or "Bank").strip()[:120] or "Bank"
    link.access_token_enc = seal(access_token)
    link.status, link.last_error = "ok", None
    db.flush()
    _log(db, "bank_linked", link.institution)
    return link


def disconnect(db: Session, client: PlaidClient, link: BankLink) -> None:
    """Stops the feed for one bank: tells Plaid (which also ends its monthly charge) and drops the
    bank's transactions. The Money page accounts stay, with their last balances."""
    token = unseal(link.access_token_enc)
    if token:
        try:
            client.remove(token)
        except PlaidError as exc:
            if exc.code not in ("ITEM_NOT_FOUND", "INVALID_ACCESS_TOKEN"):
                raise
    ids = [a.id for a in db.query(BankAccount).filter(BankAccount.link_id == link.id).all()]
    if ids:
        db.query(BankTransaction).filter(BankTransaction.bank_account_id.in_(ids)).delete(synchronize_session=False)
        db.query(BankAccount).filter(BankAccount.id.in_(ids)).delete(synchronize_session=False)
    _log(db, "bank_removed", link.institution)
    db.delete(link)
    db.flush()
