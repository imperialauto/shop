from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import RepairOrder
from app.auth import get_current_user
from app.config import GROQ_API_KEY
from groq import Groq

router = APIRouter()


def require_user(request, db):
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def get_vehicle_context(ro: RepairOrder) -> str:
    v = ro.vehicle
    year = v.year or "Unknown year"
    make = v.make or "Unknown make"
    model = v.model or "Unknown model"
    engine = v.engine or ""
    mileage = f"{ro.mileage_in:,} miles" if ro.mileage_in else "unknown mileage"
    engine_str = f" ({engine})" if engine else ""
    return f"{year} {make} {model}{engine_str} at {mileage}"


def call_claude(prompt: str) -> str:
    if not GROQ_API_KEY:
        return "AI assist not configured. Set GROQ_API_KEY in your environment."
    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content


@router.post("/suggest-diagnostic/{ro_id}", response_class=HTMLResponse)
def suggest_diagnostic(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return HTMLResponse("<p class='text-red-400'>Not authenticated</p>")

    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        return HTMLResponse("<p class='text-red-400'>RO not found</p>")

    vehicle_ctx = get_vehicle_context(ro)
    concern = ro.concern or "No concern listed"
    notes = ro.tech_notes or "No tech notes yet"

    prompt = f"""You are a master diesel and automotive technician with deep expertise in Ford Powerstroke (6.0L, 6.7L), Cummins, Duramax, Mercedes Sprinter, and commercial fleet vehicles.

Vehicle: {vehicle_ctx}
Customer concern: {concern}
Tech notes so far: {notes}

Provide a structured diagnostic approach:
1. Most likely causes (ranked by probability for this specific platform)
2. Key tests/checks to perform first
3. Any platform-specific failure patterns or known issues to watch for
4. Estimated diagnostic time

Be specific to the vehicle platform. Keep it practical and actionable for a working tech."""

    result = call_claude(prompt)
    return HTMLResponse(f'<div class="prose prose-invert max-w-none text-sm whitespace-pre-wrap">{result}</div>')


@router.post("/draft-line-items/{ro_id}", response_class=HTMLResponse)
def draft_line_items(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return HTMLResponse("<p class='text-red-400'>Not authenticated</p>")

    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        return HTMLResponse("<p class='text-red-400'>RO not found</p>")

    vehicle_ctx = get_vehicle_context(ro)
    notes = ro.tech_notes or "No tech notes"

    prompt = f"""You are a shop management assistant for an automotive repair shop specializing in diesel and fleet vehicles.

Vehicle: {vehicle_ctx}
Tech notes / work performed: {notes}

Convert these tech notes into invoice line items. Format as a markdown table with columns:
| Description | Type | Qty | Unit Price |

Pricing guidelines:
- Diesel labor: $150/hr
- Gas/other labor: $130/hr
- Parts markup: 25-30% over cost
- Type is one of: labor, part, sublet, misc

Be specific with descriptions. Separate labor and parts into individual line items."""

    result = call_claude(prompt)
    return HTMLResponse(f'<div class="prose prose-invert max-w-none text-sm">{result}</div>')


@router.post("/suggest-upsells/{ro_id}", response_class=HTMLResponse)
def suggest_upsells(request: Request, ro_id: int, db: Session = Depends(get_db)):
    user = require_user(request, db)
    if not user:
        return HTMLResponse("<p class='text-red-400'>Not authenticated</p>")

    ro = db.query(RepairOrder).filter(RepairOrder.id == ro_id).first()
    if not ro:
        return HTMLResponse("<p class='text-red-400'>RO not found</p>")

    vehicle_ctx = get_vehicle_context(ro)
    concern = ro.concern or ""
    notes = ro.tech_notes or ""

    prompt = f"""You are an experienced diesel shop service advisor. Your job is to identify legitimate upsell and maintenance opportunities based on the vehicle in the shop.

Vehicle: {vehicle_ctx}
Current concern: {concern}
Work being performed: {notes}

Suggest 3-5 relevant additional services or inspections to recommend to the customer. Focus on:
- Mileage-appropriate preventive maintenance
- Platform-specific known wear items (e.g., FICM on 6.0L Powerstroke, CP4 fuel pump on 6.7L Cummins, transfer case fluid on Sprinter)
- Items that make sense to do while the vehicle is already apart
- High-value, high-likelihood failure items for this platform

For each suggestion include: what to check/do, why it matters for this specific vehicle, and approximate add-on revenue.
Keep it honest — only suggest things that genuinely benefit the customer."""

    result = call_claude(prompt)
    return HTMLResponse(f'<div class="prose prose-invert max-w-none text-sm whitespace-pre-wrap">{result}</div>')
