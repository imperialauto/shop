"""Owner-approved labor rates. Saved values in Business settings win over these defaults.

Defaults come from the master price sheet (docs/PRICE-SHEET.md, Nov 8 2026).
No parts markup is applied anywhere: none has been approved.
"""
import json

from app import business_settings

RATE_LABELS = {
    "shop": "In-shop labor (per hour)",
    "mobile": "Mobile labor (per hour)",
    "diesel": "Diesel labor (per hour)",
    "cspr": "Customer-supplied parts labor (per hour)",
    "service_call": "Mobile service call (flat, plus travel)",
}
DEFAULT_RATES = {"shop": 150.0, "mobile": 165.0, "diesel": 175.0, "cspr": 165.0, "service_call": 75.0}


def rates() -> dict:
    try:
        saved = json.loads(business_settings.raw("labor_rates") or "{}")
    except ValueError:
        saved = {}
    merged = dict(DEFAULT_RATES)
    for key, value in (saved if isinstance(saved, dict) else {}).items():
        if key in DEFAULT_RATES and isinstance(value, (int, float)) and 0 <= value <= 1000:
            merged[key] = float(value)
    return merged


def rate_for(kind: str) -> float:
    current = rates()
    return current.get(kind if kind in ("shop", "mobile", "diesel", "cspr") else "shop", current["shop"])
