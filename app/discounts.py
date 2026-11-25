"""Discounts on a job: an amount off that shows on the estimate, approval page and invoice.

A discount is a line with item type "discount", a negative price and no tax, the same kind of line
the referral and next-visit offers already add. The shop gives it, so it is approved by the shop
when it is added: the customer sees it on their approval page as money off, not as an item to
approve or decline. Nothing here sends anything to anyone.

If the customer declines every charged item on the job, its discounts are declined with them, so a
job never ends up owing the customer money.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import LineItem, RepairOrder

DISCOUNT = "discount"


class DiscountError(ValueError):
    pass


def is_discount(li: LineItem) -> bool:
    return li.item_type == DISCOUNT


def _charges(ro: RepairOrder) -> float:
    """What the job charges before discounts and tax: every line still in play (not declined)."""
    return round(sum(li.total for li in ro.line_items
                     if li.approval != "declined" and not is_discount(li) and (li.unit_price or 0) > 0), 2)


def _discounts(ro: RepairOrder) -> float:
    return round(-sum(li.total for li in ro.line_items if is_discount(li) and li.approval != "declined"), 2)


def add(db: Session, ro: RepairOrder, description: str, amount: float, actor: str) -> LineItem:
    """Adds `amount` dollars off the job. `amount` is the positive number of dollars off.
    Caller commits. Raises DiscountError when it can't be added."""
    description = (description or "").strip()
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise DiscountError("amount") from None
    if not description:
        raise DiscountError("description")
    if not amount > 0:
        raise DiscountError("amount")
    if amount + _discounts(ro) > _charges(ro) + 0.005:
        raise DiscountError("too_big")
    now = datetime.now(timezone.utc)
    line = LineItem(repair_order_id=ro.id, description=description[:2000], item_type=DISCOUNT, quantity=1.0,
                    unit_price=-amount, taxable=False, approval="approved", decided_at=now,
                    approval_source="shop", approval_recorded_by=(actor or "shop")[:64])
    db.add(line)
    db.flush()
    if line not in ro.line_items:
        ro.line_items.append(line)
    return line


def follow_decisions(ro: RepairOrder) -> None:
    """After the customer decides: if nothing charged is left on the job, its discounts go too."""
    if any(li.approval in ("approved", "pending", None) and not is_discount(li) and (li.unit_price or 0) > 0
           for li in ro.line_items):
        return
    now = datetime.now(timezone.utc)
    for li in ro.line_items:
        if is_discount(li) and li.approval == "approved" and li.approval_source == "shop":
            li.approval, li.decided_at = "declined", now


def money(value) -> str:
    """$1,234.50 or -$75.00 (never $-75.00)."""
    value = round(float(value or 0), 2)
    return ("-$" if value < 0 else "$") + f"{abs(value):,.2f}"
