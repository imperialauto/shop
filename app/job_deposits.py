"""Deposits on a job before it has an invoice (owner's rule: 50% down on every job).

A deposit is recorded on the job with its amount, how it was paid and the day it came in. It texts nobody.
When the job's invoice is created, every deposit still open becomes a payment on that invoice, so the invoice
balance and the pickup text already count it. A deposit recorded once an invoice exists goes straight onto
that invoice as a payment too.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Invoice, JobDeposit, Payment, RepairOrder

DUPLICATE_DAY_NOTE = "A deposit like that is already recorded for that day. Add a reference if this is a second one."


def open_invoice(ro: RepairOrder) -> Invoice | None:
    return next((i for i in ro.invoices if i.status != "void"), None)


def active(ro: RepairOrder) -> list[JobDeposit]:
    """Deposits on the job that weren't removed, oldest first."""
    return [d for d in ro.deposits if d.removed_at is None]


def total(ro: RepairOrder) -> float:
    return round(sum(d.amount or 0 for d in active(ro)), 2)


def unapplied_total(ro: RepairOrder) -> float:
    """Deposits not on an invoice yet (they count as paid wherever the invoice's payments do)."""
    return round(sum(d.amount or 0 for d in active(ro) if d.payment_id is None), 2)


def job_total(ro: RepairOrder) -> float:
    """What the job comes to today: lines that are approved or not yet decided, with tax on parts."""
    from app import deposit_requests
    lines = [li for li in ro.line_items if li.approval != "declined"]
    return deposit_requests.amounts(lines)[0] if lines else 0.0


def summary(ro: RepairOrder) -> dict:
    """What the job page shows: deposits, the 50% that's asked for, and what's left on the job."""
    from app.deposit_requests import DEPOSIT_SHARE
    job = job_total(ro)
    paid = total(ro)
    due = round(job * DEPOSIT_SHARE, 2)
    return {"deposits": active(ro), "paid": paid, "job_total": job, "deposit_due": due,
            "short": round(max(due - paid, 0), 2), "left_on_job": round(max(job - paid, 0), 2)}


def _payment_for(invoice: Invoice, dep: JobDeposit) -> Payment:
    note = f"Deposit #{dep.id}" + (f": {dep.reference}" if dep.reference else "")
    pay = Payment(invoice_id=invoice.id, amount=dep.amount, method=dep.method, reference=note[:128],
                  paid_at=dep.received_at)
    invoice.payments.append(pay)
    return pay


def _settle(invoice: Invoice) -> None:
    """Paid in full closes the invoice. A draft stays a draft otherwise: recording a deposit doesn't send it."""
    if invoice.balance <= 0:
        invoice.status = "paid"


def apply_to_invoice(db: Session, invoice: Invoice) -> int:
    """Carries every open deposit on the invoice's job onto it as a payment. Returns how many were carried."""
    ro = invoice.repair_order
    carried = 0
    for dep in active(ro) if ro is not None else []:
        if dep.payment_id is not None:
            continue
        pay = _payment_for(invoice, dep)
        db.flush()
        dep.payment_id = pay.id
        carried += 1
    if carried:
        _settle(invoice)
    return carried


def record_error(ro: RepairOrder, amount: float, method: str, reference: str, received_at: datetime) -> str | None:
    """Why this deposit can't be recorded, or None when it is fine."""
    from app.routes.invoices import enabled_methods, money_error, _shop_day
    if ro.status in ("delivered", "cancelled"):
        return "This job is already picked up. Record money on its invoice instead."
    if method not in enabled_methods():
        return "Choose a valid payment method."
    problem = money_error(amount)
    if problem:
        return problem
    amount = round(amount, 2)
    invoice = open_invoice(ro)
    ceiling = invoice.balance if invoice is not None else round(job_total(ro) - total(ro), 2)
    if ceiling <= 0:
        return "Nothing is left to pay on this job."
    if amount > ceiling:
        return f"That is more than the ${ceiling:,.2f} left on this job."
    for dep in active(ro):
        if reference and dep.reference == reference:
            return "That reference was already recorded."
        if (not reference and dep.amount == amount and dep.method == method
                and _shop_day(dep.received_at) == _shop_day(received_at)):
            return DUPLICATE_DAY_NOTE
    return None


def record(db: Session, ro: RepairOrder, amount: float, method: str, reference: str = "", paid_on: str = "",
           by: str = "", before_commit=None) -> tuple[JobDeposit | None, str | None]:
    """Records a deposit on the job. Returns (deposit, None) or (None, why not). Commits on success."""
    from app.routes.invoices import paid_on_to_utc
    now = datetime.utcnow()
    received_at, error = paid_on_to_utc(paid_on, now)
    received_at = received_at or now
    reference = (reference or "").strip()[:128]
    method = (method or "").strip().lower()
    error = error or record_error(ro, amount, method, reference, received_at)
    if error:
        return None, error
    dep = JobDeposit(repair_order_id=ro.id, amount=round(amount, 2), method=method, reference=reference or None,
                     received_at=received_at, recorded_by=(by or "")[:64] or None)
    ro.deposits.append(dep)
    db.flush()
    invoice = open_invoice(ro)
    if invoice is not None:
        apply_to_invoice(db, invoice)
    if before_commit is not None:
        before_commit(dep)
    db.commit()
    return dep, None


def as_json(dep: JobDeposit) -> dict:
    return {"id": dep.id, "job_id": dep.repair_order_id, "amount": dep.amount, "method": dep.method,
            "reference": dep.reference, "received_at": dep.received_at.isoformat() + "Z",
            "on_invoice": dep.payment_id is not None}


def remove(db: Session, ro: RepairOrder, dep_id: int, by: str = "") -> str | None:
    """Takes back a deposit recorded by mistake, while it isn't on an invoice yet. Returns why not, or None."""
    dep = next((d for d in active(ro) if d.id == dep_id), None)
    if dep is None:
        return "That deposit isn't on this job."
    if dep.payment_id is not None:
        return "That deposit is already on the invoice. Change it on the invoice."
    dep.removed_at = datetime.now(timezone.utc)
    dep.removed_by = (by or "")[:64] or None
    db.commit()
    return None
