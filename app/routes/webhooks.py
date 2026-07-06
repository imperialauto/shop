from fastapi import APIRouter, Request, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.config import OPENPHONE_SIGNING_SECRET, OPENPHONE_API_KEY, GROQ_API_KEY, OWNER_PHONE, OPENPHONE_PHONE_NUMBER_ID
from app.calendar_utils import create_appointment
import hmac, hashlib, base64, json, httpx, secrets, datetime
from groq import AsyncGroq

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
    # Use env var override if set (most reliable)
    if OPENPHONE_PHONE_NUMBER_ID:
        return OPENPHONE_PHONE_NUMBER_ID
    if _phone_number_id_cache:
        return _phone_number_id_cache
    if not OPENPHONE_API_KEY:
        print("[SMS] No OPENPHONE_API_KEY set — cannot look up phone number ID")
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
        print("[SMS] Could not get phone number ID — from field will be missing")
    return None


async def send_sms(to: str, body: str) -> dict:
    if not OPENPHONE_API_KEY:
        print(f"[SMS skipped — no API key] To: {to} | {body[:80]}")
        return {"error": "no api key"}

    # Normalize to E.164
    to = "".join(c for c in to if c.isdigit() or c == "+")
    if not to.startswith("+"):
        to = "+1" + to.lstrip("1")

    phone_number_id = await get_phone_number_id()
    if not phone_number_id:
        print(f"[SMS BLOCKED] No from number — message not sent. To: {to} | {body[:80]}")
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

# ── Conversation session helpers ───────────────────────────────────────────

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

# ── Claude intake conversation ─────────────────────────────────────────────

INTAKE_SYSTEM = """You are the intake assistant for Imperial Auto Care, a diesel and fleet specialist shop in Phoenix, AZ.

Shop info you CAN answer if a customer asks:
- Address: 7915 N Glen Harbor Blvd, Suite 417, Glendale, AZ 85307
- Phone: (480) 914-4144
- Specialty: diesel engines and commercial fleet vehicles

If a customer asks something you CANNOT answer (hours, specific scheduling availability, billing, insurance, warranty policy, anything requiring a human decision), do NOT guess and do NOT repeat yourself. Instead, output ONLY this JSON on its own line:
{"forward":true,"reason":"<brief description of what they asked>"}

Your job: collect information via SMS to build an accurate repair estimate. Be friendly, professional, and BRIEF — this is SMS. One or two sentences max per reply.

You need to collect ALL of the following (in a natural conversation — don't make it feel like a form):
1. Customer name
2. Vehicle year, make, and model
3. Engine — ALWAYS confirm gas or diesel. If diesel, get the exact engine (6.0L Powerstroke, 6.7L Powerstroke, 6.4L Powerstroke, 7.3L Powerstroke, 6.6L Duramax, 5.9L or 6.7L Cummins, 3.0L Sprinter CDI, etc.)
4. Approximate mileage
5. The complaint — and ALWAYS follow up with diagnostic questions:
   - How long has this been happening?
   - Is it constant or intermittent (comes and goes)?
   - Any warning lights on the dash? Which ones?
   - Does it get worse under load, at highway speed, or when fully warmed up?
   - Any recent repairs, fluid changes, or work done on it?
   - Any unusual smells, smoke color, or noises? (describe)
   These follow-up questions are critical for diesel diagnostics — don't skip them. Work them in naturally as the customer describes the issue, 1-2 at a time.
6. Customer-supplied parts — if the customer mentions they have their own parts, want to bring their own parts, or already bought a part, note it. Do NOT make this a required question — only flag it if they bring it up.

Rules:
- Ask 1-2 questions per message — never dump everything at once
- If they give multiple pieces of info, acknowledge and only ask for what's still missing
- If their complaint is vague ("runs rough", "won't start"), ask clarifying questions before moving on
- Be conversational, not robotic — you represent the shop
- NEVER mention prices, rates, or cost estimates during the intake — that comes in the formal estimate after we have full details
- NEVER say "bring it in" or suggest scheduling before the estimate is generated — let the customer get an estimate first
- Build trust through your diagnostic questions — show you know diesel, not that you're trying to sell them something
- If the customer mentions they have their own parts or want to supply parts, warmly acknowledge it and let them know we do install customer-supplied parts (just note it internally — do NOT quote a rate in chat)
- When you have ALL items (name, year, make, model, engine, mileage, complaint with diagnostic detail), output ONLY this JSON on its own line:
{"done":true,"name":"...","year":2019,"make":"Ford","model":"F-250","engine":"6.7L Powerstroke diesel","mileage":120000,"complaint":"...","customer_supplied_parts":false}

The complaint field should include ALL diagnostic details gathered (symptoms, duration, warning lights, conditions, recent work).
Set customer_supplied_parts to true if the customer mentioned bringing/having their own parts.
Do NOT output JSON until you have the diagnostic follow-ups answered."""


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

# ── Estimate generation ────────────────────────────────────────────────────

ESTIMATE_SYSTEM = """You are a diesel and fleet repair estimator for Imperial Auto Care in Phoenix, AZ.

Standard rates: diesel/fleet $150/hr, gas $130/hr. Parts markup 25-30% over dealer cost.
Customer-supplied parts rates: diesel $165/hr, gas $145/hr. When customer_supplied_parts=true:
  - Use the higher labor rate above
  - Set parts_low and parts_high to 0 (customer is supplying them)
  - Set total_low/total_high to labor only
  - Add to notes: "Labor rate reflects customer-supplied parts. We do not warranty parts not sourced by the shop."

CRITICAL — ONE-TIME-USE (OTU) PARTS KNOWLEDGE:
You must flag any OTU parts relevant to the job in the otu_parts array. Common examples:
- 6.0L Powerstroke: head bolts (TTY), EGR cooler gaskets, oil cooler o-rings, injector cup o-rings, valley cover gasket
- 6.7L Powerstroke: EGR cooler outlet gasket, turbo pedestal o-rings, DPF/DOC gaskets, some EGR hardware
- 6.6L Duramax LLY/LBZ/LMM: head bolts (TTY), injector return line o-rings, valley cover gasket, EGR valve gasket
- 6.6L Duramax LML/L5P: CP4 high-pressure fuel lines (crimped — OTU), fuel rail return lines, head bolts
- Cummins 5.9/6.7: head bolts (TTY), injector copper washers, injector hold-down hardware, flywheel bolts (some)
- All platforms: stretch/TTY bolts of any kind, copper crush washers, exhaust manifold bolts (heat-stressed, always inspect), combustion seal washers

Respond ONLY with this JSON (no other text):
{
  "summary": "One-line description of the work",
  "labor_hours": 4.5,
  "labor_rate": 150,
  "labor_total": 675.0,
  "parts_low": 800,
  "parts_high": 1100,
  "total_low": 1475,
  "total_high": 1775,
  "notes": "Important caveats, things to watch for, recommended upsells",
  "needs_inspection": false,
  "customer_supplied_parts": false,
  "otu_parts": ["6.0L head bolts (TTY — must replace)", "EGR cooler gaskets (one-time-use)"]
}

Set needs_inspection=true only when you genuinely cannot estimate without seeing the vehicle.
Use real flat-rate labor times. otu_parts should be an empty array [] if no OTU parts apply."""


async def generate_estimate(data: dict) -> dict:
    client = AsyncGroq(api_key=GROQ_API_KEY)

    csp = data.get("customer_supplied_parts", False)
    prompt = (
        f"Generate a repair estimate:\n"
        f"Vehicle: {data.get('year')} {data.get('make')} {data.get('model')} — {data.get('engine')}\n"
        f"Mileage: {data.get('mileage')}\n"
        f"Complaint: {data.get('complaint')}\n"
        f"Customer: {data.get('name')}\n"
        f"customer_supplied_parts: {'true' if csp else 'false'}"
    )

    response = await client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=700,
        messages=[
            {"role": "system", "content": ESTIMATE_SYSTEM},
            {"role": "user", "content": prompt},
        ],
    )

    text = response.choices[0].message.content.strip()
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

# ── Appointment date parser ────────────────────────────────────────────────

YES_WORDS = {"yes", "y", "yes!", "yep", "yeah", "ya", "sure", "ok", "okay", "yup", "absolutely", "let's do it", "lets do it"}


async def forward_to_owner(from_number: str, body: str, context: str = ""):
    """Tell the customer a human will follow up and ping the owner with the message."""
    await send_sms(
        from_number,
        "I'll get someone from the shop to follow up with you directly — give us just a moment. "
        "You can also reach us at (480) 914-4144.",
    )
    if OWNER_PHONE:
        note = f"\nContext: {context}" if context else ""
        await send_sms(
            OWNER_PHONE,
            f"📨 Message needs your attention\nFrom: {from_number}\n\"{body}\"{note}",
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
                    "Output ONLY valid JSON — no other text: "
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

    if not from_number or not body:
        print(f"[WEBHOOK] missing from_number or body — skipping. from={from_number!r} body={body!r}")
        return JSONResponse({"status": "ok"})

    # Load or create conversation session
    session = get_active_session(db, from_number)

    # ── Already forwarded or scheduled — pass message to owner ────────────
    if session and session.status in ("forwarded", "scheduled"):
        if OWNER_PHONE:
            label = "Follow-up" if session.status == "forwarded" else "Post-appt question"
            await send_sms(
                OWNER_PHONE,
                f"📨 {label} from {from_number}:\n\"{body}\"",
            )
        # Let the customer know a human will respond
        await send_sms(
            from_number,
            "Got it — I'll pass that along to the shop. Someone will follow up with you shortly. "
            "You can also reach us at (480) 914-4144.",
        )
        return JSONResponse({"status": "ok"})

    # ── Scheduling reply ───────────────────────────────────────────────────
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
        cal_summary = f"Drop-off: {name} — {year} {make} {model}"
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
            f"Please plan to drop off by that time. See you then! — Imperial Auto Care (480) 914-4144",
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

    # ── Customer replied after estimate sent (draft_ready) ─────────────────
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
            # Forward to owner instead of looping — they have a question
            await forward_to_owner(from_number, body, context="customer has a pending estimate")
            session.status = "forwarded"
            db.commit()
        return JSONResponse({"status": "ok"})

    # ── New or active intake conversation ──────────────────────────────────
    if session is None:
        session = create_session(db, from_number)

    conversation = json.loads(session.conversation or "[]")

    # Run one turn of the intake conversation
    reply, collected, should_forward = await intake_turn(conversation, body)

    # ── Bot doesn't know — forward to owner ───────────────────────────────
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

        csp = collected.get("customer_supplied_parts", False) or estimate.get("customer_supplied_parts", False)

        if estimate.get("needs_inspection"):
            customer_msg = (
                f"Hi {name}! Thanks for the details on your {year} {make} {model}. "
                f"Based on what you've described, we'd need to do a hands-on inspection before "
                f"we can give you a solid number — there are a few things that can only be confirmed in person. "
                f"No charge for the inspection. Reply YES to set up a drop-off time, "
                f"or call us at (480) 914-4144!"
            )
        else:
            summary = estimate.get("summary", "")
            notes = estimate.get("notes", "")
            labor_hrs = estimate.get("labor_hours", 0)
            labor_rate = estimate.get("labor_rate", 150)
            labor_total = estimate.get("labor_total", 0)
            parts_low = estimate.get("parts_low", 0)
            parts_high = estimate.get("parts_high", 0)
            total_low = estimate.get("total_low", 0)
            total_high = estimate.get("total_high", 0)

            parts_line = (
                "Parts: You're supplying — no parts charge from us\n"
                if csp
                else f"Parts: ${parts_low:,.0f}–${parts_high:,.0f}\n"
            )
            csp_note = (
                "\nNote: Since you're supplying your own parts, we use our customer-supplied rate. "
                "We also can't warranty parts we didn't source — just so you know upfront.\n"
                if csp else ""
            )

            customer_msg = (
                f"Hi {name}! Here's what we're looking at for your {year} {make} {model}:\n\n"
                f"{summary}\n\n"
                f"Labor: {labor_hrs} hrs @ ${labor_rate}/hr = ${labor_total:,.0f}\n"
                f"{parts_line}"
                f"Estimated total: ${total_low:,.0f}–${total_high:,.0f}\n"
                f"{csp_note}"
                f"{notes}\n\n"
                f"These are ballpark numbers — final price confirmed once we're hands-on. "
                f"Reply YES to schedule a drop-off, or call us at (480) 914-4144!"
            )

        await send_sms(from_number, customer_msg)

        # Notify Jaelan
        if OWNER_PHONE:
            otu = estimate.get("otu_parts", [])
            otu_line = f"\n⚠ OTU: {', '.join(otu)}" if otu else ""
            owner_msg = (
                f"New estimate\n"
                f"{name} ({from_number})\n"
                f"{year} {make} {model} — {collected.get('engine')}\n"
                f"Job: {collected.get('complaint')}\n"
                f"Est: ${estimate.get('total_low', 0):,.0f}–${estimate.get('total_high', 0):,.0f}"
                f"{otu_line}\n"
                f"Review: https://web-production-94989.up.railway.app/ro/{ro.id}"
            )
            await send_sms(OWNER_PHONE, owner_msg)

    else:
        # Still collecting info — send next question
        db.commit()
        await send_sms(from_number, reply)

    return JSONResponse({"status": "ok"})
