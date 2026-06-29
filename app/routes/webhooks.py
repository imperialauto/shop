from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import OPENPHONE_SIGNING_SECRET, OPENPHONE_API_KEY, ANTHROPIC_API_KEY, OWNER_PHONE
import hmac, hashlib, base64, json, httpx, secrets
import anthropic

router = APIRouter()

# ── Signature verification ─────────────────────────────────────────────────

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return True
    key = base64.b64decode(secret)
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

# ── Quo/OpenPhone API ──────────────────────────────────────────────────────

_phone_number_id_cache: str | None = None

async def get_phone_number_id() -> str | None:
    """Fetch the first phone number ID from the workspace (cached)."""
    global _phone_number_id_cache
    if _phone_number_id_cache:
        return _phone_number_id_cache
    if not OPENPHONE_API_KEY:
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.openphone.com/v1/phone-numbers",
            headers={"Authorization": f"Bearer {OPENPHONE_API_KEY}"},
        )
        if resp.status_code == 200:
            numbers = resp.json().get("data", [])
            if numbers:
                _phone_number_id_cache = numbers[0]["id"]
                return _phone_number_id_cache
    return None


async def send_sms(to: str, body: str) -> dict:
    if not OPENPHONE_API_KEY:
        print(f"[SMS skipped — no API key] To: {to} | {body}")
        return {"error": "no api key"}

    # Normalize to E.164
    to = "".join(c for c in to if c.isdigit() or c == "+")
    if not to.startswith("+"):
        to = "+1" + to.lstrip("1")

    phone_number_id = await get_phone_number_id()
    payload: dict = {"to": [to], "content": body}
    if phone_number_id:
        payload["from"] = phone_number_id

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openphone.com/v1/messages",
            headers={"Authorization": f"Bearer {OPENPHONE_API_KEY}"},
            json=payload,
        )
        return resp.json()

# ── Conversation session helpers ───────────────────────────────────────────

def get_active_session(db: Session, phone: str):
    from app.models import EstimateSession
    return (
        db.query(EstimateSession)
        .filter(EstimateSession.phone_number == phone, EstimateSession.status == "collecting")
        .order_by(EstimateSession.created_at.desc())
        .first()
    )


def create_session(db: Session, phone: str):
    from app.models import EstimateSession
    session = EstimateSession(phone_number=phone, conversation="[]", collected_data="{}")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session

# ── Claude intake conversation ─────────────────────────────────────────────

INTAKE_SYSTEM = """You are the intake assistant for Imperial Auto Care, a diesel and fleet specialist shop in Phoenix, AZ.

Your job: collect information via SMS to generate a repair estimate. Be friendly, professional, and BRIEF — this is SMS.

You need to collect ALL of the following:
1. Customer name
2. Vehicle year, make, and model
3. Engine — ALWAYS ask specifically: gas or diesel? If diesel, exact engine code (6.0L Powerstroke, 6.7L Powerstroke, 6.6L Duramax, Cummins 6.7, etc.)
4. Approximate mileage
5. What they need done or what's wrong (the complaint)

Rules:
- Ask 1-2 questions per message, max — never dump all questions at once
- If they give multiple pieces of info in one message, acknowledge it and only ask for what's still missing
- Be conversational, not robotic
- When you have ALL five items confirmed, output ONLY this JSON on its own line (nothing else before or after):
{"done":true,"name":"...","year":2019,"make":"Ford","model":"F-250","engine":"6.7L Powerstroke diesel","mileage":120000,"complaint":"..."}

Do NOT output the JSON until you have all five items."""


async def intake_turn(conversation: list, new_message: str) -> tuple[str, dict | None]:
    """One turn of the intake conversation. Returns (reply, collected_data_or_None)."""
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    messages = conversation + [{"role": "user", "content": new_message}]

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=INTAKE_SYSTEM,
        messages=messages,
    )

    reply = response.content[0].text.strip()

    # Check if Claude signalled completion with JSON
    collected = None
    if '"done":true' in reply or '"done": true' in reply:
        try:
            start = reply.find("{")
            end = reply.rfind("}") + 1
            collected = json.loads(reply[start:end])
        except (json.JSONDecodeError, ValueError):
            pass

    return reply, collected

# ── Estimate generation ────────────────────────────────────────────────────

ESTIMATE_SYSTEM = """You are a diesel and fleet repair estimator for Imperial Auto Care in Phoenix, AZ.
Rates: diesel/fleet $150/hr, gas $130/hr. Parts markup 25-30% over dealer cost.

Respond ONLY with this JSON:
{
  "summary": "One-line description of the work",
  "labor_hours": 4.5,
  "labor_rate": 150,
  "labor_total": 675.0,
  "parts_low": 800,
  "parts_high": 1100,
  "total_low": 1475,
  "total_high": 1775,
  "notes": "Important caveats, things to watch for, recommended additional services",
  "needs_inspection": false
}

Set needs_inspection=true only when you genuinely cannot estimate without seeing the vehicle.
Use real flat-rate labor times. Apply diesel expertise for Powerstroke, Duramax, and Cummins jobs."""


async def generate_estimate(data: dict) -> dict:
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

    prompt = (
        f"Generate a repair estimate:\n"
        f"Vehicle: {data.get('year')} {data.get('make')} {data.get('model')} — {data.get('engine')}\n"
        f"Mileage: {data.get('mileage')}\n"
        f"Complaint: {data.get('complaint')}\n"
        f"Customer: {data.get('name')}"
    )

    response = await client.messages.create(
        model="claude-opus-4-8",
        max_tokens=600,
        system=ESTIMATE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    return json.loads(text[start:end])

# ── Mileage parsing ────────────────────────────────────────────────────────

def parse_mileage(raw) -> int:
    s = str(raw).lower().replace(",", "").replace(" ", "").replace("miles", "").replace("mi", "")
    if s.endswith("k"):
        try:
            return int(float(s[:-1]) * 1000)
        except ValueError:
            return 0
    try:
        return int(float(s))
    except ValueError:
        return 0

# ── Draft RO creation ──────────────────────────────────────────────────────

def create_draft_ro(db: Session, session, data: dict, estimate: dict):
    from app.models import Customer, Vehicle, RepairOrder, LineItem, Communication

    # Find or create customer by phone
    customer = db.query(Customer).filter(Customer.phone == session.phone_number).first()
    if not customer:
        parts = data.get("name", "Unknown").split(maxsplit=1)
        customer = Customer(
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
            phone=session.phone_number,
        )
        db.add(customer)
        db.flush()

    mileage = parse_mileage(data.get("mileage", 0))

    vehicle = Vehicle(
        customer_id=customer.id,
        year=int(data.get("year", 0)),
        make=data.get("make", ""),
        model=data.get("model", ""),
        engine=data.get("engine", ""),
        mileage=mileage,
    )
    db.add(vehicle)
    db.flush()

    ro = RepairOrder(
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        status="waiting_approval",
        concern=data.get("complaint", ""),
        tech_notes=(
            f"SMS Estimate Request — {session.phone_number}\n"
            f"Engine: {data.get('engine')}\n"
            f"Mileage: {data.get('mileage')}\n\n"
            f"AI Notes: {estimate.get('notes', '')}"
        ),
        public_token=secrets.token_urlsafe(16),
        mileage_in=mileage,
    )
    db.add(ro)
    db.flush()

    if not estimate.get("needs_inspection"):
        db.add(LineItem(
            repair_order_id=ro.id,
            description=f"Labor — {estimate.get('summary', data.get('complaint'))}",
            item_type="labor",
            quantity=estimate.get("labor_hours", 0),
            unit_price=estimate.get("labor_rate", 150),
        ))
        parts_mid = (estimate.get("parts_low", 0) + estimate.get("parts_high", 0)) / 2
        if parts_mid > 0:
            db.add(LineItem(
                repair_order_id=ro.id,
                description="Parts (estimate — subject to final quote)",
                item_type="part",
                quantity=1,
                unit_price=parts_mid,
            ))

    db.add(Communication(
        repair_order_id=ro.id,
        customer_id=customer.id,
        direction="inbound",
        channel="sms",
        body=f"SMS estimate intake from {session.phone_number}. Complaint: {data.get('complaint')}",
    ))

    db.commit()
    db.refresh(ro)
    return ro, customer

# ── Webhook endpoint ───────────────────────────────────────────────────────

@router.post("/openphone")
async def openphone_webhook(
    request: Request,
    x_openphone_signature: str = Header(None),
    db: Session = Depends(get_db),
):
    payload = await request.body()

    if OPENPHONE_SIGNING_SECRET and x_openphone_signature:
        if not verify_signature(payload, x_openphone_signature, OPENPHONE_SIGNING_SECRET):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(payload)

    if data.get("type") != "message.received":
        return JSONResponse({"status": "ok"})

    msg = data.get("data", {}).get("object", {})

    # Skip outbound messages (bot's own replies)
    if msg.get("direction") == "outbound":
        return JSONResponse({"status": "ok"})

    from_number = msg.get("from", "").strip()
    body = msg.get("body", "").strip()

    if not from_number or not body:
        return JSONResponse({"status": "ok"})

    # Load or create conversation session
    session = get_active_session(db, from_number)
    if session is None:
        session = create_session(db, from_number)

    conversation = json.loads(session.conversation or "[]")

    # Run one turn of the intake conversation
    reply, collected = await intake_turn(conversation, body)

    # Update conversation history
    conversation.append({"role": "user", "content": body})
    conversation.append({"role": "assistant", "content": reply})
    session.conversation = json.dumps(conversation)

    if collected and collected.get("done"):
        # All info collected — generate estimate
        session.status = "generating"
        session.collected_data = json.dumps(collected)
        db.commit()

        await send_sms(from_number, "Perfect, I have everything I need! Generating your estimate now — give me just a moment.")

        try:
            estimate = await generate_estimate(collected)
        except Exception as e:
            print(f"[Estimate generation error] {e}")
            await send_sms(from_number, "I ran into an issue generating your estimate. Someone from the shop will follow up with you shortly.")
            session.status = "error"
            db.commit()
            return JSONResponse({"status": "ok"})

        ro, customer = create_draft_ro(db, session, collected, estimate)
        session.status = "draft_ready"
        session.draft_ro_id = ro.id
        db.commit()

        # Send estimate to customer
        name = collected.get("name", "there")
        year = collected.get("year")
        make = collected.get("make")
        model = collected.get("model")

        if estimate.get("needs_inspection"):
            customer_msg = (
                f"Hi {name}! Based on what you've described, we'll need to inspect the vehicle "
                f"before we can give you an accurate estimate. Give us a call or swing by — "
                f"we're Imperial Auto Care in Phoenix. We'll get you taken care of!"
            )
        else:
            customer_msg = (
                f"Hi {name}! Here's your estimate for the {year} {make} {model}:\n\n"
                f"{estimate.get('summary')}\n\n"
                f"Labor: {estimate.get('labor_hours')} hrs @ ${estimate.get('labor_rate')}/hr = ${estimate.get('labor_total'):,.0f}\n"
                f"Parts: ${estimate.get('parts_low'):,.0f}–${estimate.get('parts_high'):,.0f}\n"
                f"Total est: ${estimate.get('total_low'):,.0f}–${estimate.get('total_high'):,.0f}\n\n"
                f"{estimate.get('notes', '')}\n\n"
                f"Ready to schedule? Reply YES or call us!"
            )

        await send_sms(from_number, customer_msg)

        # Notify Jaelan
        if OWNER_PHONE:
            owner_msg = (
                f"New estimate\n"
                f"{name} ({from_number})\n"
                f"{year} {make} {model} — {collected.get('engine')}\n"
                f"Job: {collected.get('complaint')}\n"
                f"Est: ${estimate.get('total_low', 0):,.0f}–${estimate.get('total_high', 0):,.0f}\n"
                f"Review: https://web-production-94989.up.railway.app/ro/{ro.id}"
            )
            await send_sms(OWNER_PHONE, owner_msg)

    else:
        # Still collecting info — send next question
        db.commit()
        await send_sms(from_number, reply)

    return JSONResponse({"status": "ok"})
