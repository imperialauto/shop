"""Starts the estimate for a website request from approved prices only.

Nothing here is a final price. It adds what the price list already approves (the
diagnostic, at its catalog price) as an unsent line, names the labor rate that applies,
and says what still needs checking. Parts, labor hours and travel are never guessed.
"""
from app import pricing
from app.models import LineItem, RepairOrder
from app.public_site import approved_bookable_services

DIAGNOSE_FIRST = {"auto-repair", "diesel", "european-sprinter", "dps6-transmission", "fleet"}
CUSTOM_QUOTE = {"towing", "mobile-roadside", "rentals", "nvzn-customs"}


def rate_kind(line_slug: str, where: str) -> str:
    if line_slug == "diesel":
        return "diesel"
    return "mobile" if where in ("onsite", "roadside") else "shop"


def prefill(db, order: RepairOrder, line_slug: str, where: str) -> str:
    """Adds approved lines to the job (not sent to the customer) and returns one line for the owner."""
    if line_slug in CUSTOM_QUOTE:
        return "Custom quote: location, distance, vehicle size and equipment decide the price. Nothing is prefilled."
    notes = []
    diagnostic = next((s for s in approved_bookable_services() if s.slug == "diagnostic"), None)
    if line_slug in DIAGNOSE_FIRST and where == "shop" and diagnostic and diagnostic.price is not None:
        db.add(LineItem(repair_order_id=order.id, item_type="labor", quantity=1, unit_price=diagnostic.price, taxable=False,
                        description="Diagnostic (approved price list). Confirm the scope before sending for approval."))
        notes.append(f"Prefilled: diagnostic ${diagnostic.price:,.2f}.".replace(".00.", "."))
    kind = rate_kind(line_slug, where)
    rate = pricing.rate_for(kind)
    notes.append(f"Labor rate: ${rate:,.0f}/hr ({kind}).")
    if kind == "mobile":
        notes.append(f"Mobile service call ${pricing.rates()['service_call']:,.0f} plus travel (travel not set; confirm).")
    notes.append("Check the VIN, ALLDATA labor time and current parts prices before sending.")
    return " ".join(notes)
