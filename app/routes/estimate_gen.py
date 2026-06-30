from fastapi import APIRouter, Request, HTTPException, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
import anthropic, json, base64, os, secrets

from app.database import get_db
from app.auth import get_current_user
from app.config import ANTHROPIC_API_KEY

router = APIRouter()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "templates")
)

EXTRACT_ESTIMATE_SYSTEM = """You are a repair estimator for Imperial Auto Care, a diesel and fleet specialist shop in Phoenix, AZ.
Rates: diesel/fleet $150/hr, gas $130/hr. Parts markup 25-30% over dealer cost.

The shop owner has shared a customer conversation (pasted text or a screenshot). Extract vehicle info and generate an estimate.

Output ONLY this JSON (nothing else before or after):
{
  "name": "customer name or Unknown",
  "year": 2019,
  "make": "Ford",
  "model": "F-250",
  "engine": "6.7L Powerstroke diesel",
  "mileage": 120000,
  "complaint": "what the customer wants done",
  "missing_info": [],
  "summary": "one-line job description",
  "labor_hours": 4.5,
  "labor_rate": 150,
  "labor_total": 675.0,
  "parts_low": 800,
  "parts_high": 1100,
  "total_low": 1475,
  "total_high": 1775,
  "notes": "important caveats, diesel-specific watch items, recommended upsells",
  "needs_inspection": false
}

Rules:
- If mileage unknown, use 0 and add "mileage" to missing_info
- Add any fields you had to guess to missing_info
- needs_inspection=true only when you genuinely cannot estimate without seeing the vehicle
- Use real flat-rate labor times (Mitchell/AllData standards)
- Apply diesel expertise for Powerstroke, Duramax, Cummins"""


@router.get("/", response_class=HTMLResponse)
async def estimate_gen_page(request: Request, db: Session = Depends(get_db)):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(
        "estimates/generate.html", {"request": request, "user": user}
    )


@router.post("/generate", response_class=HTMLResponse)
async def generate_from_input(
    request: Request,
    db: Session = Depends(get_db),
    conversation_text: str = Form(default=""),
    screenshot: UploadFile = File(default=None),
):
    try:
        user = get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    # Build message content — image takes priority over text
    if screenshot and screenshot.filename:
        img_bytes = await screenshot.read()
        img_b64 = base64.standard_b64encode(img_bytes).decode()
        media_type = screenshot.content_type or "image/jpeg"
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_b64,
                },
            },
            {
                "type": "text",
                "text": "Read this screenshot of a customer text conversation. Extract the vehicle info and complaint, then generate the repair estimate.",
            },
        ]
    elif conversation_text.strip():
        content = [{"type": "text", "text": f"Customer conversation:\n\n{conversation_text.strip()}"}]
    else:
        return HTMLResponse(
            '<p class="text-red-400 text-sm">Paste a conversation or upload a screenshot first.</p>'
        )

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    try:
        response = await client.messages.create(
            model="claude-opus-4-8",
            max_tokens=900,
            system=EXTRACT_ESTIMATE_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        text = response.content[0].text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        data = json.loads(text[start:end])
    except Exception as e:
        return HTMLResponse(
            f'<p class="text-red-400 text-sm">Error generating estimate: {e}</p>'
        )

    estimate_json = json.dumps(data)

    missing = data.get("missing_info", [])
    missing_html = ""
    if missing:
        missing_html = f"""
        <div class="bg-yellow-900 bg-opacity-30 border border-yellow-700 rounded p-3 mb-4 text-sm text-yellow-300">
          ⚠ Heads up — I had to guess or couldn't find: <strong>{', '.join(missing)}</strong>. Double-check before sending.
        </div>"""

    if data.get("needs_inspection"):
        estimate_block = """
        <div class="bg-plum-900 border border-plum-600 rounded p-4 text-stone-300 text-sm">
          <p class="font-semibold text-gold-300 mb-1">Inspection Required</p>
          <p>Can't give a solid number without seeing the vehicle first. Let them know to bring it in.</p>
        </div>"""
    else:
        lh = data.get("labor_hours", 0)
        lr = data.get("labor_rate", 150)
        lt = data.get("labor_total", 0)
        pl = data.get("parts_low", 0)
        ph = data.get("parts_high", 0)
        tl = data.get("total_low", 0)
        th = data.get("total_high", 0)
        estimate_block = f"""
        <div class="grid grid-cols-2 gap-3 text-sm mb-4">
          <div class="bg-plum-900 rounded p-3">
            <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Labor</div>
            <div class="text-stone-200">{lh} hrs @ ${lr}/hr</div>
            <div class="text-gold-300 font-semibold">${lt:,.0f}</div>
          </div>
          <div class="bg-plum-900 rounded p-3">
            <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Parts</div>
            <div class="text-stone-200">${pl:,.0f} – ${ph:,.0f}</div>
          </div>
          <div class="bg-plum-900 rounded p-3 col-span-2">
            <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Total Estimate</div>
            <div class="text-gold-400 font-bold text-lg">${tl:,.0f} – ${th:,.0f}</div>
          </div>
        </div>"""

    notes_html = ""
    if data.get("notes"):
        notes_html = f"""
        <div class="text-xs text-stone-400 bg-plum-900 rounded p-3 mb-4">
          <span class="font-semibold text-stone-300">Notes:</span> {data['notes']}
        </div>"""

    year = data.get("year", "")
    make = data.get("make", "")
    model = data.get("model", "")
    engine = data.get("engine", "")
    mileage = data.get("mileage", 0)
    name = data.get("name", "Unknown")
    complaint = data.get("complaint", "")
    summary = data.get("summary", complaint)

    return HTMLResponse(f"""
    <div class="card space-y-4">
      <div class="flex items-center justify-between">
        <h2 class="text-gold-300 font-semibold">Estimate Ready</h2>
      </div>

      {missing_html}

      <div class="grid grid-cols-2 gap-3 text-sm">
        <div>
          <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Customer</div>
          <div class="text-stone-200 font-medium">{name}</div>
        </div>
        <div>
          <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Vehicle</div>
          <div class="text-stone-200 font-medium">{year} {make} {model}</div>
          <div class="text-stone-400 text-xs">{engine}</div>
        </div>
        <div>
          <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Mileage</div>
          <div class="text-stone-200">{mileage:,} mi</div>
        </div>
        <div>
          <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Complaint</div>
          <div class="text-stone-200">{complaint}</div>
        </div>
      </div>

      <div>
        <div class="text-stone-400 text-xs uppercase tracking-wide mb-2">Job</div>
        <div class="text-stone-200 text-sm font-medium">{summary}</div>
      </div>

      {estimate_block}
      {notes_html}

      <form hx-post="/estimates/create-ro" hx-target="#ro-result" hx-swap="innerHTML" class="border-t border-plum-700 pt-4">
        <input type="hidden" name="estimate_json" value='{estimate_json}' />
        <div class="flex items-end gap-3">
          <div class="flex-1">
            <label class="form-label">Customer phone (optional)</label>
            <input type="text" name="phone_number" placeholder="+16025551234" class="form-input" />
          </div>
          <button type="submit" class="btn-primary whitespace-nowrap">Create Draft RO</button>
        </div>
        <div id="ro-result" class="mt-3"></div>
      </form>
    </div>
    """)


@router.post("/create-ro", response_class=HTMLResponse)
async def create_ro_from_estimate(
    request: Request,
    db: Session = Depends(get_db),
    estimate_json: str = Form(...),
    phone_number: str = Form(default=""),
):
    try:
        get_current_user(request, db)
    except HTTPException:
        return RedirectResponse("/login", status_code=302)

    data = json.loads(estimate_json)

    from app.models import Customer, Vehicle, RepairOrder, LineItem

    customer = None
    if phone_number.strip():
        normalized = "".join(c for c in phone_number if c.isdigit() or c == "+")
        if not normalized.startswith("+"):
            normalized = "+1" + normalized.lstrip("1")
        customer = db.query(Customer).filter(Customer.phone == normalized).first()

    if not customer:
        parts = data.get("name", "Unknown").split(maxsplit=1)
        customer = Customer(
            first_name=parts[0],
            last_name=parts[1] if len(parts) > 1 else "",
            phone=phone_number.strip() or "",
        )
        db.add(customer)
        db.flush()

    mileage = int(data.get("mileage", 0) or 0)
    vehicle = Vehicle(
        customer_id=customer.id,
        year=int(data.get("year", 0) or 0),
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
        tech_notes=f"Manual estimate — AI notes: {data.get('notes', '')}",
        public_token=secrets.token_urlsafe(16),
        mileage_in=mileage,
    )
    db.add(ro)
    db.flush()

    if not data.get("needs_inspection"):
        db.add(LineItem(
            repair_order_id=ro.id,
            description=f"Labor — {data.get('summary', data.get('complaint'))}",
            item_type="labor",
            quantity=float(data.get("labor_hours", 0) or 0),
            unit_price=float(data.get("labor_rate", 150) or 150),
        ))
        parts_mid = (
            float(data.get("parts_low", 0) or 0) + float(data.get("parts_high", 0) or 0)
        ) / 2
        if parts_mid > 0:
            db.add(LineItem(
                repair_order_id=ro.id,
                description="Parts (estimate — subject to final quote)",
                item_type="part",
                quantity=1,
                unit_price=parts_mid,
            ))

    db.commit()
    db.refresh(ro)

    return HTMLResponse(f"""
        <div class="bg-green-900 bg-opacity-30 border border-green-700 rounded p-3 flex items-center justify-between">
          <span class="text-green-300 text-sm font-semibold">✓ Draft RO #{ro.id} created</span>
          <a href="/ro/{ro.id}" class="btn-primary text-xs">Open RO →</a>
        </div>
    """)
