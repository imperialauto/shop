"""Starts the estimate for a website request from approved prices only.

Nothing here is a final price. It adds what the price list already approves (the
diagnostic, at its catalog price) as an unsent line, names the labor rate that applies,
and says what still needs checking. Parts, labor hours and travel are never guessed.
"""
from app import pricing
from app.models import LineItem, RepairOrder
from app.public_site import approved_bookable_services

DIAGNOSE_FIRST = {"auto-repair", "diesel", "european-sprinter", "dps6-transmission", "fleet"}
CUSTOM_QUOTE = {"towing", "mobile-roadside", "heavy-duty", "rentals", "nvzn-customs"}


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


OIL_CHANGE = "oil-change"
OIL_CHANGE_LINE = ("Full synthetic oil change special: up to 5 qt full synthetic oil, oil filter and multi-point inspection. "
                   "Extra quarts or a special filter are quoted before work.")


def oil_change_line(db, order: RepairOrder, service) -> LineItem | None:
    """A booked oil change special starts its job with the one package line at the saved price.
    The line is not sent for approval; the owner checks the oil capacity and filter first.
    Tax goes on top of the price (owner's choice, Nov 22). The package is one flat price with no labor
    listed separately, so it is taxed as a part, the whole line, like parts on any other job."""
    if service is None or service.slug != OIL_CHANGE or service.price is None:
        return None
    line = LineItem(repair_order_id=order.id, item_type="part", quantity=1, unit_price=service.price, taxable=True,
                    description=OIL_CHANGE_LINE)
    db.add(line)
    return line
