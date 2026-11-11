from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import OPENPHONE_SIGNING_SECRET, OPENPHONE_API_KEY, GROQ_API_KEY, OWNER_PHONE, OPENPHONE_PHONE_NUMBER_ID
from app.calendar_utils import create_appointment
from app.sending_pause import sending_allowed
import hmac, hashlib, base64, json, httpx, os, secrets, datetime
from groq import AsyncGroq

router = APIRouter()

# â”€â”€ Signature verification â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return True
    key = base64.b64decode(secret)
    expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)

# â”€â”€ Quo/OpenPhone API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_phone_number_id_cache: str | None = None

async def get_phone_number_id() -> str | None:
    """Fetch the first phone number ID from the workspace (cached)."""
    global _phone_number_id_cache
    # Use env var override if set (most reliable)
    if OPENPHONE_PHONE_NUMBER_ID:
        return OPENPHONE_PHONE_NUMBER_ID
    if _phone_number_id_cache:
        return _phone_number_id_cache
    if not OPENPHONE_API_KEY:
        print("[SMS] No OPENPHONE_API_KEY set â€” cannot look up phone number ID")
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.openphone.com/v1/phone-numbers",
            headers={"Authorization": OPENPHONE_API_KEY},
        )
        print(f"[SMS] phone-numbers lookup: status={resp.status_code} body={resp.text[:300]}")
        if resp.status_code == 200:
            numbers = resp.json().get("data", [])
            if numbers:
                _phone_number_id_cache = numbers[0]["id"]
                print(f"[SMS] using phone number id: {_phone_number_id_cache}")
                return _phone_number_id_cache
        print("[SMS] Could not get phone number ID â€” from field will be missing")
    return None


async def send_sms(to: str, body: str) -> dict:
    if not OPENPHONE_API_KEY:
        print(f"[SMS skipped â€” no API key] To: {to} | {body[:80]}")
        return {"error": "no api key"}
    if not sending_allowed():
        print("[SMS BLOCKED] Texting is stopped by the owner.")
        return {"error": "sending paused"}

    # Normalize to E.164
    to = "".join(c for c in to if c.isdigit() or c == "+")
    if not to.startswith("+"):
        to = "+1" + to.lstrip("1")

    phone_number_id = await get_phone_number_id()
    if not phone_number_id:
        print(f"[SMS BLOCKED] No from number â€” message not sent. To: {to} | {body[:80]}")
        return {"error": "no from number"}

    payload: dict = {"from": phone_number_id, "to": [to], "content": body}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.openphone.com/v1/messages",
            headers={"Authorization": OPENPHONE_API_KEY},
            json=payload,
        )
        print(f"[SMS] status={resp.status_code} from={phone_number_id} to={to} response={resp.text[:300]}")
        return resp.json()

# â”€â”€ Conversation session helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_active_session(db: Session, phone: str):
    from app.models import EstimateSession
    return (
        db.query(EstimateSession)
        .filter(
            EstimateSession.phone_number == phone,
            EstimateSession.status.in_(["collecting", "draft_ready", "scheduling", "forwarded", "scheduled"]),
        )
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

# â”€â”€ Claude intake conversation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

INTAKE_SYSTEM = """You are the intake assistant for Imperial Auto Care, a diesel and fleet specialist shop in Phoenix, AZ.

Shop info you CAN answer if a customer asks:
- Address: 7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307
- Phone: (480) 914-4144
- Specialty: diesel engines and commercial fleet vehicles

If a customer asks something you CANNOT answer (hours, specific scheduling availability, billing, insurance, warranty policy, anything requiring a human decision), do NOT guess and do NOT repeat yourself. Instead, output ONLY this JSON on its own line:
{"forward":true,"reason":"<brief description of what they asked>"}

Your job: collect information via SMS to build an accurate repair estimate. Be friendly, professional, and BRIEF â€” this is SMS. One or two sentences max per reply.

You need to collect ALL of the following (in a natural conversation â€” don't make it feel like a form):
1. Customer name
2. Vehicle year, make, and model
3. Engine â€” ALWAYS confirm gas or diesel. If diesel, get the exact engine (6.0L Powerstroke, 6.7L Powerstroke, 6.4L Powerstroke, 7.3L Powerstroke, 6.6L Duramax, 5.9L or 6.7L Cummins, 3.0L Sprinter CDI, etc.)
4. Approximate mileage
5. The complaint â€” and ALWAYS follow up with diagnostic questions:
   - How long has this been happening?
   - Is it constant or intermittent (comes and goes)?
   - Any warning lights on the dash? Which ones?
   - Does it get worse under load, at highway speed, or when fully warmed up?
   - Any recent repairs, fluid changes, or work done on it?
   - Any unusual smells, smoke color, or noises? (describe)
   These follow-up questions are critical for diesel diagnostics â€” don't skip them. Work them in naturally as the customer describes the issue, 1-2 at a time.
6. Customer-supplied parts â€” if the customer mentions they have their own parts, want to bring their own parts, or already bought a part, note it. Do NOT make this a required question â€” only flag it if they bring it up.

Rules:
- Ask 1-2 questions per message â€” never dump everything at once
- If they give multiple pieces of info, acknowledge and only ask for what's still missing
- If their complaint is vague ("runs rough", "won't start"), ask clarifying questions before moving on
- Be conversational, not robotic â€” you represent the shop
- NEVER mention prices, rates, or cost estimates during the intake â€” that comes in the formal estimate after we have full details
- NEVER say "bring it in" or suggest scheduling before the estimate is generated â€” let the customer get an estimate first
- Build trust through your diagnostic questions â€” show you know diesel, not that you're trying to sell them something
- If the customer mentions they have their own parts or want to supply parts, warmly acknowledge it and let them know we do install customer-supplied parts (just note it internally â€” do NOT quote a rate in chat)
- When you have ALL items (name, year, make, model, engine, mileage, complaint with diagnostic detail), output ONLY this JSON on its own line:
{"done":true,"name":"...","year":2019,"make":"Ford","model":"F-250","engine":"6.7L Powerstroke diesel","mileage":120000,"complaint":"...","customer_supplied_parts":false}

The complaint field should include ALL diagnostic details gathered (symptoms, duration, warning lights, conditions, recent work).
Set customer_supplied_parts to true if the customer mentioned bringing/having their own parts.
Do NOT output JSON until you have the diagnostic follow-ups answered.

If you see a message starting with "Customer sent a photo showing:", that means a customer sent an image and it was automatically analyzed. Use that information as part of the intake â€” acknowledge it naturally ("Got it, I can see the codes from your photo â€” let me ask a couple follow-up questions...") and continue collecting any missing info."""


async def intake_turn(conversation: list, new_message: str) -> tuple[str, dict | None, bool]:
    """One turn of the intake conversation. Returns (reply, collected_data_or_None, should_forward)."""
    client = AsyncGroq(api_key=GROQ_API_KEY)

    messages = [{"role": "system", "content": INTAKE_SYSTEM}] + conversation + [{"role": "user", "content": new_message}]

    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=300,
        messages=messages,
    )

    reply = response.choices[0].message.content.strip()

    # Check if bot wants to forward to owner
    should_forward = False
    if '"forward":true' in reply or '"forward": true' in reply:
        try:
            start = reply.find("{")
            end = reply.rfind("}") + 1
            fwd = json.loads(reply[start:end])
            if fwd.get("forward"):
                should_forward = True
        except (json.JSONDecodeError, ValueError):
            pass

    # Check if bot signalled intake completion with JSON
    collected = None
    if not should_forward and ('"done":true' in reply or '"done": true' in reply):
        try:
            start = reply.find("{")
            end = reply.rfind("}") + 1
            collected = json.loads(reply[start:end])
        except (json.JSONDecodeError, ValueError):
            pass

    return reply, collected, should_forward

# â”€â”€ Estimate generation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# The legacy OpenPhone intake is deprecated and off by default. Never let
# an AI invent parts markup, labor rates or a retail quote. The supported
# estimating path is the existing repair order and verified price sheet.
async def generate_estimate(data: dict) -> dict:
    raise RuntimeError("Automatic SMS pricing is disabled; staff must verify labor and parts before quoting.")


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

# â”€â”€ Draft RO creation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
        status="intake" if estimate.get("needs_inspection") else "waiting_approval",
        concern=data.get("complaint", ""),
        tech_notes=(
            f"SMS Estimate Request â€” {session.phone_number}\n"
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
            description=f"Labor â€” {estimate.get('summary', data.get('complaint'))}",
            item_type="labor",
            quantity=estimate.get("labor_hours", 0),
            unit_price=estimate.get("labor_rate", 150),
        ))
        parts_mid = (estimate.get("parts_low", 0) + estimate.get("parts_high", 0)) / 2
        if parts_mid > 0:
            db.add(LineItem(
                repair_order_id=ro.id,
                description="Parts (estimate â€” subject to final quote)",
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

# â”€â”€ Appointment date parser â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

YES_WORDS = {"yes", "y", "yes!", "yep", "yeah", "ya", "sure", "ok", "okay", "yup", "absolutely", "let's do it", "lets do it"}


async def forward_to_owner(from_number: str, body: str, context: str = ""):
    """Tell the customer a human will follow up and ping the owner with the message."""
    await send_sms(
        from_number,
        "I'll get someone from the shop to follow up with you directly â€” give us just a moment. "
        "You can also reach us at (480) 914-4144.",
    )
    if OWNER_PHONE:
        note = f"\nContext: {context}" if context else ""
        await send_sms(
            OWNER_PHONE,
            f"ðŸ“¨ Message needs your attention\nFrom: {from_number}\n\"{body}\"{note}",
        )


async def parse_appointment_date(user_input: str) -> datetime.datetime | None:
    """Use Groq to parse a natural-language date/time into a datetime. Returns None if unclear."""
    client = AsyncGroq(api_key=GROQ_API_KEY)
    today = datetime.date.today()

    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=80,
        messages=[
            {
                "role": "system",
                "content": (
                    f"Today is {today.strftime('%A, %B %d, %Y')}. The shop is in Phoenix, AZ (Mountain Standard Time, no DST). "
                    "Parse the user's requested appointment date/time. "
                    "If a time is missing, default to 08:00. "
                    "Output ONLY valid JSON â€” no other text: "
                    '{"date":"YYYY-MM-DD","time":"HH:MM","valid":true} or {"valid":false} if the input is too unclear to parse.'
                ),
            },
            {"role": "user", "content": user_input},
        ],
    )

    text = response.choices[0].message.content.strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        parsed = json.loads(text[start:end])
        if parsed.get("valid") and parsed.get("date") and parsed.get("time"):
            return datetime.datetime.strptime(f"{parsed['date']} {parsed['time']}", "%Y-%m-%d %H:%M")
    except Exception as e:
        print(f"[DateParser] Failed to parse '{user_input}': {e} | raw={text}")
    return None


def fmt_appt(dt: datetime.datetime) -> str:
    """Format a datetime for SMS display, e.g. 'Tuesday, July 8 at 8:00 AM'"""
    hour = dt.hour % 12 or 12
    minute = dt.strftime("%M")
    ampm = "AM" if dt.hour < 12 else "PM"
    return dt.strftime(f"%A, %B %-d at {hour}:{minute} {ampm}")



# â”€â”€ Vision: describe an image URL using Groq llama-4-scout â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def describe_image(url: str) -> str:
    """Download image and ask a vision model to describe it for auto intake context."""
    try:
        client = AsyncGroq(api_key=GROQ_API_KEY)
        resp = await client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": url},
                        },
                        {
                            "type": "text",
                            "text": (
                                "You are helping an auto repair shop intake bot. "
                                "Describe what you see in this image as it relates to a vehicle problem. "
                                "If you see diagnostic trouble codes (DTCs), list them all. "
                                "If you see a dashboard warning light, name it. "
                                "If you see physical damage, describe it concisely. "
                                "Keep your response under 3 sentences. "
                                "Start with: \'Customer sent a photo showing:\'"
                            ),
                        },
                    ],
                }
            ],
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[VISION] error describing image: {e}")
        return "[Customer sent a photo â€” could not analyze it automatically]"


# â”€â”€ Webhook endpoint â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@router.post("/openphone")
async def openphone_webhook(
    request: Request,
    x_openphone_signature: str = Header(None),
    db: Session = Depends(get_db),
):
    payload = await request.body()

    # Reject unsigned requests: missing signature must never bypass verification.
    if not OPENPHONE_SIGNING_SECRET:
        raise HTTPException(status_code=503, detail="Webhook verification unavailable")
    if not x_openphone_signature or not verify_signature(payload, x_openphone_signature, OPENPHONE_SIGNING_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # The messaging service owns customer texts. This older AI intake would text
    # customers on its own, so it stays off unless deliberately switched on.
    if os.getenv("LEGACY_SMS_INTAKE_ENABLED") != "true":
        return JSONResponse({"status": "ignored"})

    data = json.loads(payload)
    print(f"[WEBHOOK] type={data.get('type')} keys={list(data.keys())} preview={str(data)[:400]}")

    if data.get("type") != "message.received":
        print(f"[WEBHOOK] skipping event type: {data.get('type')}")
        return JSONResponse({"status": "ok"})

    msg = data.get("data", {}).get("object", {})
    print(f"[WEBHOOK] msg keys={list(msg.keys())} direction={msg.get('direction')} from={msg.get('from')} body={str(msg.get('body',''))[:100]}")

    # Skip outbound messages (bot's own replies)
    if msg.get("direction") == "outbound":
        return JSONResponse({"status": "ok"})

    from_number = msg.get("from", "").strip()
    body = msg.get("body", "").strip()
    media = msg.get("media", [])

    if not from_number:
        print(f"[WEBHOOK] missing from_number â€” skipping.")
        return JSONResponse({"status": "ok"})

    # â”€â”€ MMS image handling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if media:
        image_urls = [m.get("url") for m in media if m.get("url") and "image" in m.get("type", "")]
        if image_urls:
            vision_desc = await describe_image(image_urls[0])
            print(f"[WEBHOOK] image received â€” vision description: {vision_desc[:120]}")
            # Prepend vision description to any text the customer also sent
            body = vision_desc + (f"\n\nCustomer also wrote: {body}" if body else "")

    if not body:
        print(f"[WEBHOOK] empty body, no media â€” skipping.")
        return JSONResponse({"status": "ok"})

    # Load or create conversation session
    session = get_active_session(db, from_number)

    # â”€â”€ Already forwarded or scheduled â€” pass message to owner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if session and session.status in ("forwarded", "scheduled"):
        if OWNER_PHONE:
            label = "Follow-up" if session.status == "forwarded" else "Post-appt question"
            await send_sms(
                OWNER_PHONE,
                f"ðŸ“¨ {label} from {from_number}:\n\"{body}\"",
            )
        # Let the customer know a human will respond
        await send_sms(
            from_number,
            "Got it â€” I'll pass that along to the shop. Someone will follow up with you shortly. "
            "You can also reach us at (480) 914-4144.",
        )
        return JSONResponse({"status": "ok"})

    # â”€â”€ Scheduling reply â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if session and session.status == "scheduling":
        appt_dt = await parse_appointment_date(body)
        if appt_dt is None:
            await send_sms(
                from_number,
                "Sorry, I didn't catch that. What day and time works for your drop-off? "
                "(e.g. 'Monday at 8am' or 'July 10th at 2pm')",
            )
            return JSONResponse({"status": "ok"})

        # Create calendar event
        collected = json.loads(session.collected_data or "{}")
        name = collected.get("name", "Customer")
        year = collected.get("year", "")
        make = collected.get("make", "")
        model = collected.get("model", "")
        cal_summary = f"Drop-off: {name} â€” {year} {make} {model}"
        cal_desc = f"Phone: {from_number}\nRO: #{session.draft_ro_id}\nConcern: {collected.get('complaint', '')}"
        create_appointment(cal_summary, cal_desc, appt_dt, duration_hours=2.0)

        # Update RO promised_date
        if session.draft_ro_id:
            from app.models import RepairOrder
            ro = db.get(RepairOrder, session.draft_ro_id)
            if ro:
                ro.promised_date = appt_dt
                db.commit()

        appt_str = fmt_appt(appt_dt)
        await send_sms(
            from_number,
            f"You're all set! We have you down for {appt_str}. "
            f"Please plan to drop off by that time. See you then! â€” Imperial Auto Care (480) 914-4144",
        )

        # Notify Jaelan
        if OWNER_PHONE:
            await send_sms(
                OWNER_PHONE,
                f"Appt booked: {name} ({from_number})\n"
                f"{year} {make} {model}\n"
                f"{appt_str}\n"
                f"RO: https://web-production-94989.up.railway.app/ro/{session.draft_ro_id}",
            )

        session.status = "scheduled"
        db.commit()
        return JSONResponse({"status": "ok"})

    # â”€â”€ Customer replied after estimate sent (draft_ready) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if session and session.status == "draft_ready":
        if body.strip().lower() in YES_WORDS:
            session.status = "scheduling"
            db.commit()
            await send_sms(
                from_number,
                "Great! What day and time works for your drop-off? "
                "(e.g. 'Monday at 8am', 'this Thursday afternoon', 'July 10th at 2pm')",
            )
        else:
            # Forward to owner instead of looping â€” they have a question
            await forward_to_owner(from_number, body, context="customer has a pending estimate")
            session.status = "forwarded"
            db.commit()
        return JSONResponse({"status": "ok"})

    # â”€â”€ New or active intake conversation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if session is None:
        session = create_session(db, from_number)

    conversation = json.loads(session.conversation or "[]")

    # Run one turn of the intake conversation
    reply, collected, should_forward = await intake_turn(conversation, body)

    # â”€â”€ Bot doesn't know â€” forward to owner â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    if should_forward:
        await forward_to_owner(from_number, body, context="active intake session")
        session.status = "forwarded"
        db.commit()
        return JSONResponse({"status": "ok"})

    # Update conversation history
    conversation.append({"role": "user", "content": body})
    conversation.append({"role": "assistant", "content": reply})
    session.conversation = json.dumps(conversation)

    if collected and collected.get("done"):
        # Preserve the intake in the existing RO system without guessed
        # prices, unapproved free inspections or customer-price promises.
        session.collected_data = json.dumps(collected)
        draft = {
            "needs_inspection": True,
            "notes": "Manual pricing review required. No labor or parts quote approved.",
        }
        ro, customer = create_draft_ro(db, session, collected, draft)
        session.status = "awaiting_price_review"
        session.draft_ro_id = ro.id
        db.commit()
        await send_sms(
            from_number,
            "Thanks, we received your vehicle details. Our team will review the parts and labor "
            "and send a written estimate for your approval. No work starts without your OK.",
        )
        if OWNER_PHONE:
            await send_sms(
                OWNER_PHONE,
                f"Estimate intake needs manual pricing review: RO #{ro.id}. "
                "Verify parts, labor and customer price before sending an approval link.",
            )

    else:
        # Still collecting info â€” send next question
        db.commit()
        await send_sms(from_number, reply)

    return JSONResponse({"status": "ok"})
