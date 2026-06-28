from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from app.config import OPENPHONE_SIGNING_SECRET, OPENPHONE_API_KEY
import hmac, hashlib, base64, json, httpx

router = APIRouter()


def verify_openphone_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify OpenPhone webhook HMAC-SHA256 signature."""
    if not secret:
        return True  # Skip verification in dev if secret not set
    key = base64.b64decode(secret)
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/openphone")
async def openphone_webhook(request: Request, x_openphone_signature: str = Header(None)):
    payload = await request.body()

    if OPENPHONE_SIGNING_SECRET and x_openphone_signature:
        if not verify_openphone_signature(payload, x_openphone_signature, OPENPHONE_SIGNING_SECRET):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(payload)
    event_type = data.get("type", "")

    if event_type == "message.received":
        msg = data.get("data", {}).get("object", {})
        from_number = msg.get("from", "")
        body = msg.get("body", "")
        # TODO: Match to customer by phone number and log communication
        print(f"Inbound SMS from {from_number}: {body}")

    return JSONResponse({"status": "ok"})


async def send_sms(to: str, body: str) -> dict:
    """Send an outbound SMS via OpenPhone API."""
    if not OPENPHONE_API_KEY:
        return {"error": "OpenPhone API key not configured"}

    # Normalize phone number
    to = "".join(c for c in to if c.isdigit() or c == "+")
    if not to.startswith("+"):
        to = "+1" + to.lstrip("1")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openphone.com/v1/messages",
            headers={"Authorization": f"Bearer {OPENPHONE_API_KEY}"},
            json={"to": [to], "content": body},
        )
        return resp.json()
