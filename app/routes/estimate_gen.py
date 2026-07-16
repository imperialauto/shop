from fastapi import APIRouter, Request, HTTPException, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from groq import AsyncGroq
import json, base64, os, secrets, io
from xml.sax.saxutils import escape as xml_escape
from datetime import date

from app.database import get_db
from app.auth import get_current_user
from app.config import GROQ_API_KEY

router = APIRouter()
templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "..", "templates")
)

EXTRACT_ESTIMATE_SYSTEM = """You are a repair estimator for Imperial Auto Care, a full-service auto repair shop in Phoenix, AZ that serves everyday daily-driver customers as well as diesel and commercial fleet vehicles.
Rates: diesel/fleet $150/hr, everyday gas vehicle $130/hr. Parts markup 25-30% over dealer cost. Regular everyday customers (non-diesel, non-fleet) are common — treat their vehicles with the same care and rigor, just using the gas labor rate and skipping diesel-only sections below.

The shop owner has shared a customer conversation (pasted text or a screenshot). Extract vehicle info and generate an estimate.

CRITICAL — ONE-TIME-USE (OTU) PARTS KNOWLEDGE:
You must flag any OTU/TTY parts relevant to this job. Common examples by platform:
- 6.0L Powerstroke: head bolts (TTY), EGR cooler gaskets, oil cooler o-rings, injector cup o-rings, valley cover gasket
- 6.7L Powerstroke: EGR cooler outlet gasket, turbo pedestal o-rings, DPF/DOC gaskets, EGR valve gasket
- 6.6L Duramax LLY/LBZ/LMM: head bolts (TTY), injector return line o-rings, valley cover gasket, EGR valve gasket
- 6.6L Duramax LML/L5P: CP4 high-pressure fuel lines (crimped OTU), fuel rail return lines, head bolts (TTY)
- Cummins 5.9/6.7: head bolts (TTY), injector copper crush washers, injector hold-down hardware, flywheel bolts (some)
- All platforms: any stretch/TTY bolt, copper banjo crush washers, exhaust manifold bolts (heat-stressed — always inspect/replace), combustion seal washers

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
  "needs_inspection": false,
  "otu_parts": ["6.0L head bolts (TTY — must replace)", "EGR cooler gaskets (one-time-use)"]
}

Rules:
- If mileage unknown, use 0 and add "mileage" to missing_info
- Add any fields you had to guess to missing_info
- needs_inspection=true only when you genuinely cannot estimate without seeing the vehicle
- Use real flat-rate labor times (Mitchell/AllData standards)
- Apply diesel expertise for Powerstroke, Duramax, Cummins
- otu_parts should be [] if no OTU parts apply to this job"""


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

    client = AsyncGroq(api_key=GROQ_API_KEY)
    # Use vision model for images, text model for pasted text
    model = "llama-3.2-90b-vision-preview" if (screenshot and screenshot.filename) else "llama-3.3-70b-versatile"
    try:
        response = await client.chat.completions.create(
            model=model,
            max_tokens=900,
            messages=[
                {"role": "system", "content": EXTRACT_ESTIMATE_SYSTEM},
                {"role": "user", "content": content},
            ],
        )
        text = response.choices[0].message.content.strip()
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
        <div class="bg-yellow-900 bg-opacity-30 border border-yellow-700 rounded p-3 text-sm text-yellow-300">
          ⚠ Had to guess or couldn't find: <strong>{', '.join(missing)}</strong>. Double-check before sending.
        </div>"""

    otu = data.get("otu_parts", [])
    otu_html = ""
    if otu:
        otu_items = "".join(f"<li>{p}</li>" for p in otu)
        otu_html = f"""
        <div class="bg-red-900 bg-opacity-40 border border-red-700 rounded p-3 text-sm">
          <div class="text-red-300 font-bold mb-1">🔴 ONE-TIME-USE PARTS — Order before starting</div>
          <ul class="text-red-200 text-xs space-y-0.5 list-disc list-inside">{otu_items}</ul>
        </div>"""

    if data.get("needs_inspection"):
        estimate_block = """
        <div class="bg-plum-900 border border-plum-600 rounded p-4 text-stone-300 text-sm">
          <p class="font-semibold text-gold-300 mb-1">Inspection Required</p>
          <p>Can't give a solid number without seeing the vehicle first. Let them know to bring it in.</p>
        </div>"""
    else:
        lh = float(data.get("labor_hours", 0) or 0)
        lr = float(data.get("labor_rate", 150) or 150)
        lt = float(data.get("labor_total", 0) or 0)
        pl = float(data.get("parts_low", 0) or 0)
        ph = float(data.get("parts_high", 0) or 0)
        tl = float(data.get("total_low", 0) or 0)
        th = float(data.get("total_high", 0) or 0)
        estimate_block = f"""
        <div class="bg-plum-900 rounded p-3 text-sm mb-2">
          <div class="text-stone-400 text-xs uppercase tracking-wide mb-2">Labor — adjust if needed</div>
          <div class="flex gap-2 items-center mb-1">
            <input id="lh" type="number" step="0.5" min="0" value="{lh}"
              class="form-input w-20 text-center" onchange="recalc()" />
            <span class="text-stone-400 text-xs">hrs @</span>
            <input id="lr" type="number" step="5" min="0" value="{lr}"
              class="form-input w-20 text-center" onchange="recalc()" />
            <span class="text-stone-400 text-xs">/hr =</span>
            <span id="lt" class="text-gold-300 font-semibold">${lt:,.0f}</span>
          </div>
        </div>
        <div class="grid grid-cols-2 gap-3 text-sm mb-2">
          <div class="bg-plum-900 rounded p-3">
            <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Parts</div>
            <div class="text-stone-200">${pl:,.0f} – ${ph:,.0f}</div>
          </div>
          <div class="bg-plum-900 rounded p-3">
            <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Total Estimate</div>
            <div id="total" class="text-gold-400 font-bold">${tl:,.0f} – ${th:,.0f}</div>
          </div>
        </div>
        <script>
          const parts_low={pl}, parts_high={ph};
          function recalc() {{
            const lh=parseFloat(document.getElementById('lh').value)||0;
            const lr=parseFloat(document.getElementById('lr').value)||0;
            const lt=lh*lr;
            document.getElementById('lt').textContent='$'+lt.toLocaleString('en-US',{{maximumFractionDigits:0}});
            const tl=lt+parts_low, th=lt+parts_high;
            document.getElementById('total').textContent='$'+tl.toLocaleString('en-US',{{maximumFractionDigits:0}})+' – $'+th.toLocaleString('en-US',{{maximumFractionDigits:0}});
            // Update hidden JSON for PDF/RO
            const d=JSON.parse(document.getElementById('est-json').value);
            d.labor_hours=lh; d.labor_rate=lr; d.labor_total=lt; d.total_low=tl; d.total_high=th;
            document.querySelectorAll('.est-json-input').forEach(el=>el.value=JSON.stringify(d));
          }}
        </script>"""

    notes_html = ""
    if data.get("notes"):
        notes_html = f"""
        <div class="text-xs text-stone-400 bg-plum-900 rounded p-3">
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
    <div class="card space-y-3">
      <h2 class="text-gold-300 font-semibold">Estimate Ready</h2>

      {missing_html}
      {otu_html}

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
        <div class="text-stone-400 text-xs uppercase tracking-wide mb-1">Job</div>
        <div class="text-stone-200 text-sm font-medium">{summary}</div>
      </div>

      {estimate_block}
      {notes_html}

      <!-- Hidden master JSON (updated by recalc()) -->
      <input id="est-json" type="hidden" value='{estimate_json}' />

      <div class="border-t border-plum-700 pt-3 space-y-3">
        <!-- PDF download -->
        <form method="post" action="/estimates/pdf" target="_blank">
          <input class="est-json-input" type="hidden" name="estimate_json" value='{estimate_json}' />
          <button type="submit" class="btn-secondary w-full">⬇ Download PDF Estimate</button>
        </form>

        <!-- Create RO -->
        <form hx-post="/estimates/create-ro" hx-target="#ro-result" hx-swap="innerHTML">
          <input class="est-json-input" type="hidden" name="estimate_json" value='{estimate_json}' />
          <div class="flex items-end gap-3">
            <div class="flex-1">
              <label class="form-label">Customer phone (optional)</label>
              <input type="text" name="phone_number" placeholder="+16025551234" class="form-input" />
            </div>
            <button type="submit" class="btn-primary whitespace-nowrap">Create Draft RO</button>
          </div>
        </form>
        <div id="ro-result"></div>
      </div>
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


@router.post("/pdf")
async def download_pdf(
    request: Request,
    estimate_json: str = Form(...),
):
    """Generate and return a PDF estimate."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    def _pdf_para(value, style):
        """Escape special chars and turn real line breaks into <br/> tags so
        multi-line AI text (notes, summaries, long concerns) doesn't get
        collapsed onto one run-on line, and wraps instead of overlapping."""
        safe = xml_escape(str(value or "")).replace("\r\n", "\n").replace("\n", "<br/>")
        return Paragraph(safe, style)
    

    data = json.loads(estimate_json)
    buf = io.BytesIO()

    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=inch,
        rightMargin=inch,
    )

    styles = getSampleStyleSheet()
    dark = colors.HexColor("#1a0a2e")
    gold = colors.HexColor("#d4a853")
    grey = colors.HexColor("#57534e")
    light = colors.HexColor("#e8e0d5")

        title_style = ParagraphStyle("title", parent=styles["Heading1"], textColor=dark, fontSize=20, leading=24, spaceAfter=2)
        sub_style = ParagraphStyle("sub", parent=styles["Normal"], textColor=grey, fontSize=10, leading=13, spaceAfter=12)
        label_style = ParagraphStyle("label", parent=styles["Normal"], textColor=grey, fontSize=8, leading=10, spaceAfter=2)
        value_style = ParagraphStyle("value", parent=styles["Normal"], textColor=dark, fontSize=11, leading=15, spaceAfter=8)
        note_style = ParagraphStyle("note", parent=styles["Normal"], textColor=grey, fontSize=9, leading=13, spaceAfter=6)
        cell_style = ParagraphStyle("cell", parent=styles["Normal"], textColor=dark, fontSize=9, leading=12)

    today = date.today().strftime("%B %d, %Y")
    name = data.get("name", "Customer")
    year = data.get("year", "")
    make = data.get("make", "")
    model_name = data.get("model", "")
    engine = data.get("engine", "")
    mileage = int(data.get("mileage", 0) or 0)
    complaint = data.get("complaint", "")
    summary = data.get("summary", complaint)
    needs_inspection = data.get("needs_inspection", False)

    story = []

    # Header
        story.append(Paragraph("Imperial Auto Care", title_style))
        story.append(Paragraph("Full-Service Auto Repair - Diesel, Fleet &amp; Everyday Vehicles - Phoenix, AZ", sub_style))
    story.append(Spacer(1, 0.1 * inch))

    # Info table
    info_data = [
        ["Date", today, "Customer", name],
        ["Vehicle", f"{year} {make} {model_name}", "Engine", engine],
        ["Mileage", f"{mileage:,} mi", "Concern", _pdf_para(complaint, cell_style)],
    ]
    info_table = Table(info_data, colWidths=[1.1 * inch, 2.5 * inch, 1 * inch, 2 * inch])
    info_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#57534e")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#57534e")),
        ("TEXTCOLOR", (1, 0), (1, -1), dark),
        ("TEXTCOLOR", (3, 0), (3, -1), dark),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.HexColor("#f5f0ea"), colors.white]),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(info_table)
    story.append(Spacer(1, 0.25 * inch))

    # Job summary
    story.append(Paragraph("JOB SUMMARY", label_style))
    story.append(_pdf_para(summary, value_style))
    story.append(Spacer(1, 0.1 * inch))

    # Estimate table
    if needs_inspection:
        story.append(Paragraph(
            "⚠ An in-person inspection is required before a firm estimate can be provided.",
            note_style,
        ))
    else:
        lh = data.get("labor_hours", 0)
        lr = data.get("labor_rate", 150)
        lt = data.get("labor_total", 0)
        pl = data.get("parts_low", 0)
        ph = data.get("parts_high", 0)
        tl = data.get("total_low", 0)
        th = data.get("total_high", 0)

        est_data = [
            ["Description", "Qty", "Rate", "Amount"],
            [_pdf_para(f"Labor — {summary}", cell_style), f"{lh} hrs", f"${lr}/hr", f"${lt:,.0f}"],            ["Parts & Materials (estimate)", "", "", f"${pl:,.0f} – ${ph:,.0f}"],
            ["", "", "TOTAL ESTIMATE", f"${tl:,.0f} – ${th:,.0f}"],
        ]
        est_table = Table(est_data, colWidths=[3.5 * inch, 0.8 * inch, 1.2 * inch, 1.2 * inch])
        est_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), dark),
            ("TEXTCOLOR", (0, 0), (-1, 0), gold),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.HexColor("#f5f0ea"), colors.white]),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#f0ebe0")),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(est_table)

    story.append(Spacer(1, 0.2 * inch))

    # Notes
    if data.get("notes"):
        story.append(Paragraph("NOTES & RECOMMENDATIONS", label_style))
        story.append(_pdf_para(data["notes"], note_style))
        story.append(Spacer(1, 0.1 * inch))

    # Footer disclaimer
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph(
        "This is an estimate only. Final pricing may vary based on actual parts and labor required. "
        "Prices valid for 30 days. All work performed by ASE-certified technicians.",
        ParagraphStyle("footer", parent=styles["Normal"], textColor=grey, fontSize=8),
    ))

    doc.build(story)
    buf.seek(0)

    filename = f"estimate_{name.replace(' ', '_')}_{today.replace(' ', '_')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
