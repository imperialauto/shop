"""Login protection: authenticator codes, recovery codes, and failed-login limits."""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text, func

from app.config import SECRET_KEY
from app.database import Base

ISSUER = "Imperial Operations"
STEP_SECONDS = 30
RECOVERY_CODES = 8
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60
PENDING_SECONDS = 5 * 60


class UserMfa(Base):
    __tablename__ = "user_mfa"
    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    secret_encrypted = Column(Text, nullable=False)
    recovery_hashes = Column(Text, nullable=False, default="[]")
    last_counter = Column(BigInteger, nullable=False, default=0)
    confirmed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(f"mfa:{SECRET_KEY}".encode()).digest()))


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_secret(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(base64.b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def check_code(secret: str, code: str, last_counter: int, now: float | None = None) -> int | None:
    """Returns the matched time step, or None. A step can only be used once."""
    clean = "".join(ch for ch in code if ch.isdigit())
    current = int((time.time() if now is None else now) // STEP_SECONDS)
    for counter in (current - 1, current, current + 1):
        if counter > last_counter and hmac.compare_digest(code_at(secret, counter), clean):
            return counter
    return None


def setup_link(secret: str, username: str) -> str:
    label = quote(f"{ISSUER}:{username}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits=6&period={STEP_SECONDS}"


def _hash_recovery(code: str) -> str:
    clean = code.replace("-", "").strip().lower()
    return hmac.new(SECRET_KEY.encode(), clean.encode(), hashlib.sha256).hexdigest()


def new_recovery_codes() -> tuple[list[str], str]:
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODES)]
    return codes, json.dumps([_hash_recovery(code) for code in codes])


def use_recovery_code(mfa: UserMfa, code: str) -> bool:
    hashes = json.loads(mfa.recovery_hashes or "[]")
    wanted = _hash_recovery(code)
    for stored in hashes:
        if hmac.compare_digest(stored, wanted):
            hashes.remove(stored)
            mfa.recovery_hashes = json.dumps(hashes)
            return True
    return False


class LoginLimiter:
    """Counts failures per username and per address. Resets on restart, which is acceptable
    because bcrypt already slows guessing and the limit only needs to cover a live attack."""

    def __init__(self):
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        kept = [t for t in self.failures.get(key, []) if now - t < LOCK_SECONDS]
        if kept:
            self.failures[key] = kept
        else:
            self.failures.pop(key, None)
        return kept

    def locked(self, *keys: str, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return any(len(self._recent(key, moment)) >= MAX_FAILURES for key in keys)

    def record(self, *keys: str, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        for key in keys:
            self.failures.setdefault(key, []).append(moment)

    def clear(self, *keys: str) -> None:
        for key in keys:
            self.failures.pop(key, None)


limiter = LoginLimiter()
