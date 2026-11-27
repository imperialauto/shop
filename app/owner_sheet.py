"""Owner-only profit sheet for a job: what each line costs us, what it earns, and where to get the parts.

Read only, apart from the owner saving a part's cost, part number and store. Customers and staff never see it.

Our part cost lives in the existing private sourcing records (parts_orders, status "sourcing"), matched to the
estimate line by description: the line "Part: Oil pan gasket" matches the sourcing record "Oil pan gasket", the
same convention the owner's part-cost text command already uses. No new tables or columns.
"""
from dataclasses import dataclass, field

from app import pricing
from app.models import LineItem, PartsOrder, RepairOrder

DEPOSIT_SHARE = 0.5          # owner rule, Nov 21: 50% on everything
MARGIN_TARGET = 0.50         # flag a job whose gross margin falls under this; owner can change it later
PICKUP_SPLIT = " — anticipated pickup"
OPTIONAL_PREFIX = "optional"

BUCKETS = ("labor", "parts", "diag", "supplies", "sublet", "other", "discount")
BUCKET_LABELS = {"labor": "Labor", "parts": "Parts", "diag": "Diagnostic", "supplies": "Shop supplies",
                 "sublet": "Sublet", "other": "Other", "discount": "Discounts"}


def tax_rate() -> float:
    from app.routes.invoices import SHOP_TAX_RATE
    return SHOP_TAX_RATE


def part_key(description: str | None) -> str:
    """The text a part line and its sourcing record share: no "Part:" prefix, no pickup note, any case."""
    text = (description or "").split(PICKUP_SPLIT)[0].strip()
    if text.lower().startswith("part:"):
        text = text[5:].strip()
    return text.lower()


def bucket_for(li: LineItem) -> str:
    desc = (li.description or "").lower()
    kind = (li.item_type or "labor").lower()
    if kind == "discount" or (li.unit_price or 0) < 0:
        return "discount"
    if "diag" in desc:
        return "diag"
    if "shop suppl" in desc:
        return "supplies"
    if kind == "part":
        return "parts"
    if kind == "labor":
        return "labor"
    if kind == "sublet":
        return "sublet"
    return "other"


def is_optional(li: LineItem) -> bool:
    return (li.description or "").strip().lower().startswith(OPTIONAL_PREFIX)


@dataclass
class Line:
    item: LineItem
    bucket: str
    revenue: float
    cost: float | None          # None: a part with no cost recorded yet
    source: PartsOrder | None = None
    flags: list = field(default_factory=list)

    @property
    def profit(self):
        return None if self.cost is None else round(self.revenue - self.cost, 2)

    @property
    def margin(self):
        if self.cost is None or self.revenue <= 0:
            return None
        return (self.revenue - self.cost) / self.revenue

    @property
    def hours(self):
        return self.item.quantity if self.bucket in ("labor", "diag") and self.item.item_type == "labor" else 0.0


@dataclass
class Section:
    name: str
    lines: list
    by_bucket: dict
    revenue: float
    cost: float
    profit: float
    margin: float | None
    parts_taxed: float
    tax: float
    total: float
    hours: float
    labor_dollars: float
    missing_costs: int


def _section(name: str, lines: list, rate: float) -> Section:
    by_bucket = {b: {"revenue": 0.0, "cost": 0.0, "profit": 0.0} for b in BUCKETS}
    for ln in lines:
        b = by_bucket[ln.bucket]
        b["revenue"] += ln.revenue
        b["cost"] += ln.cost or 0.0
        b["profit"] += ln.revenue - (ln.cost or 0.0)
    revenue = round(sum(ln.revenue for ln in lines), 2)
    cost = round(sum(ln.cost or 0.0 for ln in lines), 2)
    parts_taxed = round(sum(ln.revenue for ln in lines if ln.item.item_type == "part" and ln.item.taxable is not False), 2)
    tax = round(parts_taxed * rate, 2)
    hours = round(sum(ln.hours for ln in lines), 2)
    labor_dollars = round(sum(ln.revenue for ln in lines if ln.hours), 2)
    return Section(
        name=name, lines=lines, by_bucket={k: v for k, v in by_bucket.items() if v["revenue"] or v["cost"]},
        revenue=revenue, cost=cost, profit=round(revenue - cost, 2),
        margin=((revenue - cost) / revenue) if revenue > 0 else None,
        parts_taxed=parts_taxed, tax=tax, total=round(revenue + tax, 2), hours=hours, labor_dollars=labor_dollars,
        missing_costs=sum(1 for ln in lines if ln.cost is None),
    )


def what_if(sec: Section, rate: float) -> dict:
    """Numbers the owner sheet's sliders start from: parts and labor split out from everything else."""
    parts = [ln for ln in sec.lines if ln.bucket == "parts"]
    labor = [ln for ln in sec.lines if ln.bucket == "labor" and ln.item.item_type == "labor"]
    rest = [ln for ln in sec.lines if ln not in parts and ln not in labor]
    labor_hours = round(sum(ln.item.quantity or 0 for ln in labor), 2)
    labor_rev = round(sum(ln.revenue for ln in labor), 2)
    return {
        "parts_rev": round(sum(ln.revenue for ln in parts), 2),
        "parts_cost": round(sum(ln.cost or 0.0 for ln in parts), 2),
        "parts_taxed": sec.parts_taxed,
        "labor_rev": labor_rev,
        "labor_hours": labor_hours,
        "labor_rate": round(labor_rev / labor_hours, 2) if labor_hours else 0.0,
        "other_rev": round(sum(ln.revenue for ln in rest), 2),
        "other_cost": round(sum(ln.cost or 0.0 for ln in rest), 2),
        "tax_rate": rate,
        "missing_costs": sec.missing_costs,
    }


def build(db, ro: RepairOrder, margin_target: float = MARGIN_TARGET) -> dict:
    """Everything the owner sheet shows for one job."""
    rate = tax_rate()
    shop_rate = pricing.rate_for("shop")
    sources = (db.query(PartsOrder).filter(PartsOrder.repair_order_id == ro.id)
               .order_by(PartsOrder.id.desc()).all())
    by_key: dict[str, PartsOrder] = {}
    for p in sources:  # newest first, so the newest record for a part wins
        by_key.setdefault(part_key(p.description), p)

    main, optional, declined = [], [], []
    for li in sorted(ro.line_items, key=lambda x: x.id or 0):
        bucket = bucket_for(li)
        revenue = round(li.total, 2)
        src = by_key.get(part_key(li.description)) if li.item_type == "part" else None
        if li.item_type == "part":
            cost = round((src.cost or 0.0) * (li.quantity or 1), 2) if src is not None and src.cost else None
        else:
            cost = 0.0  # labor, supplies and diag carry no outside cost here
        ln = Line(item=li, bucket=bucket, revenue=revenue, cost=cost, source=src)
        if li.item_type == "part":
            if cost is None:
                ln.flags.append("No cost recorded, so margin is unknown")
            elif cost >= revenue > 0:
                ln.flags.append("Sold at or below our cost")
            if src is None or not (src.vendor or "").strip():
                ln.flags.append("No store picked yet")
        if li.item_type == "labor" and bucket == "labor" and li.quantity and 0 < (li.unit_price or 0) < shop_rate:
            ln.flags.append(f"Rate ${li.unit_price:,.2f}/hr is under the ${shop_rate:,.0f} shop rate")
        if li.approval == "declined":
            declined.append(ln)
        elif is_optional(li):
            optional.append(ln)
        else:
            main.append(ln)

    main_s = _section("Main job", main, rate)
    opt_s = _section("Optional add-ons", optional, rate) if optional else None
    declined_s = _section("Declined", declined, rate) if declined else None

    invoice = next((i for i in ro.invoices if i.status != "void"), None)
    from app import job_deposits
    paid = (invoice.paid_total if invoice else 0.0) + job_deposits.unapplied_total(ro)
    deposit_due = round(main_s.total * DEPOSIT_SHARE, 2)
    approved = any(ln.item.approval == "approved" for ln in main) or ro.status in (
        "approval_received", "in_progress", "waiting_parts", "complete", "delivered")

    job_flags = []
    if main_s.margin is not None and main_s.margin < margin_target:
        job_flags.append(f"Margin {main_s.margin:.0%} is under your {margin_target:.0%} target")
    if main_s.missing_costs:
        n = main_s.missing_costs
        job_flags.append(f"{n} part{'s' if n != 1 else ''} with no cost recorded, so the real margin is lower than it can show")
    if approved and paid < deposit_due and main_s.total > 0:
        job_flags.append("Approved, but no 50% deposit is recorded on the dashboard" if paid == 0
                         else f"Deposit short: ${paid:,.2f} recorded of ${deposit_due:,.2f}")

    parts_to_get = [ln for ln in main + optional if ln.item.item_type == "part"]
    return {
        "ro": ro, "main": main_s, "optional": opt_s, "declined": declined_s,
        "tax_rate": rate, "shop_rate": shop_rate, "margin_target": margin_target,
        "invoice": invoice, "paid": round(paid, 2), "deposit_due": deposit_due,
        "balance": round(main_s.total - paid, 2), "approved": approved,
        "job_flags": job_flags, "parts_to_get": parts_to_get,
        "effective_rate": round(main_s.labor_dollars / main_s.hours, 2) if main_s.hours else None,
        "profit_per_hour": round(main_s.profit / main_s.hours, 2) if main_s.hours else None,
        "bucket_labels": BUCKET_LABELS,
        "what_if": what_if(main_s, rate),
    }


def save_source(db, ro: RepairOrder, li: LineItem, cost: float | None, part_number: str, where: str) -> PartsOrder:
    """Record our cost, part number and store for one part line, in the job's private sourcing records."""
    key = part_key(li.description)
    record = next((p for p in db.query(PartsOrder).filter(PartsOrder.repair_order_id == ro.id)
                   .order_by(PartsOrder.id.desc()).all() if part_key(p.description) == key), None)
    if record is None:
        core = (li.description or "").strip()
        if core.lower().startswith("part:"):
            core = core[5:].strip()
        record = PartsOrder(repair_order_id=ro.id, description=core[:500], quantity=int(li.quantity or 1),
                            status="sourcing", cost=0.0)
        db.add(record)
    if cost is not None:
        record.cost = round(cost, 2)
    record.part_number = (part_number or "").strip()[:64] or record.part_number
    record.vendor = (where or "").strip()[:128] or record.vendor
    db.flush()
    return record
