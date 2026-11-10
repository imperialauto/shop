"""Short-lived handoff links for staff-assisted vehicle status recovery."""
import hashlib
import hmac

from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import SECRET_KEY
from app.models import RepairOrder

MAX_AGE_SECONDS = 30 * 60
_signer = URLSafeTimedSerializer(SECRET_KEY, salt="imperial-status-recovery-v1")


def _binding(order: RepairOrder) -> str:
    return hashlib.sha256(f"{order.public_token}|{order.customer.phone}".encode()).hexdigest()


def issue(order: RepairOrder) -> str:
    return _signer.dumps({"job": order.id, "binding": _binding(order)})


def resolve(db, ticket: str) -> RepairOrder | None:
    if len(ticket) > 512:
        return None
    try:
        payload = _signer.loads(ticket, max_age=MAX_AGE_SECONDS)
    except BadData:
        return None
    if not isinstance(payload, dict):
        return None
    job, binding = payload.get("job"), payload.get("binding")
    if type(job) is not int or not isinstance(binding, str):
        return None
    order = db.get(RepairOrder, job)
    if order and order.public_token and hmac.compare_digest(binding, _binding(order)):
        return order
    return None
