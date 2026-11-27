"""Keep a draft invoice's totals in step with its job's lines.

Lines change in many places (approval links, signed approvals, text YES, close-out, edits, removals).
Before this, only some of them re-totalled the draft invoice, so a line the customer declined could
stay on the bill. This hook re-totals any draft, unsigned invoice whose job had a line added, removed,
re-priced or decided in the same flush. Sent, signed, paid and void invoices are never touched: those
are what the customer saw.
"""
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

WATCHED = ("approval", "quantity", "unit_price", "item_type", "taxable", "repair_order_id")


def _changed(li) -> bool:
    state = inspect(li)
    return any(state.attrs[name].history.has_changes() for name in WATCHED)


@event.listens_for(Session, "before_flush")
def _retotal_drafts(session, flush_context, instances):
    from app.models import LineItem, RepairOrder

    touched: dict[int, list] = {}
    for li in list(session.new) + list(session.dirty):
        if isinstance(li, LineItem) and li.repair_order_id and (li in session.new or _changed(li)):
            # Column defaults aren't applied until insert; use the same ones so totals can be read now.
            if li.quantity is None:
                li.quantity = 1.0
            if li.unit_price is None:
                li.unit_price = 0.0
            touched.setdefault(li.repair_order_id, []).append(li)
    for li in session.deleted:
        if isinstance(li, LineItem) and li.repair_order_id:
            touched.setdefault(li.repair_order_id, [])
    if not touched:
        return

    from app.routes.invoices import totals_for
    for ro_id, extra in touched.items():
        ro = session.get(RepairOrder, ro_id)
        if ro is None:
            continue
        drafts = [inv for inv in ro.invoices if inv.status == "draft" and not inv.customer_signed_at]
        if not drafts:
            continue
        lines = [li for li in ro.line_items if li not in session.deleted]
        lines += [li for li in extra if li not in lines and li.repair_order_id == ro_id]
        totals = totals_for([li for li in lines if li.billable])
        for inv in drafts:
            if (inv.subtotal, inv.tax_rate, inv.tax_amount, inv.total) != totals:
                inv.subtotal, inv.tax_rate, inv.tax_amount, inv.total = totals
