"""Parts store receipts kept on a job: a phone photo or the store's PDF, with the store, the total and the PO.

A receipt is saved on a job either from that job's page, or from the "Match a receipt" page by its
PO (typed in, or read off the receipt when it is a PDF that prints the shop's own IAC-PO-... number).
Saving or reading one never changes a price, a part's status, the job or anything a customer sees,
and texts nobody.

Receipts live in their own table, so nothing else changes shape. The app does not create tables on
start: until migrations/20261127_parts_receipts.sql is applied, the job page shows the card as not
switched on and uploads are refused with a plain message. Every other page works as before.
"""
import base64
import json
import re
import time
import zlib
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, FetchedValue, Float, ForeignKey, Integer, LargeBinary, String, Text, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, deferred, load_only

from app import po_numbers
from app.database import Base

MAX_BYTES = 10 * 1024 * 1024
OFFICE = ("owner", "admin", "office")
STAFF = OFFICE + ("tech",)
HOW = {"job_page": "Added on this job", "po_typed": "Matched by the PO typed in", "po_on_receipt": "Matched by the PO on the receipt"}


class PartsReceipt(Base):
    __tablename__ = "parts_receipts"
    id = Column(Integer, primary_key=True)
    # Empty while a store invoice waits in the "not matched yet" list on the Match a receipt page
    # (needs migrations/20261128_parts_receipt_lines.sql; before it, every receipt is on a job).
    repair_order_id = Column(Integer, ForeignKey("repair_orders.id", ondelete="CASCADE"), nullable=True, index=True)
    vendor = Column(String(128))
    po_text = Column(String(64))  # the PO as typed or read off the receipt
    total = Column(Float)
    note = Column(Text)
    matched_by = Column(String(16), nullable=False, default="job_page")  # job_page, po_typed, po_on_receipt
    filename = Column(String(200), nullable=False)
    content_type = Column(String(64), nullable=False)
    size = Column(Integer, nullable=False)
    data = Column(LargeBinary, nullable=False)
    uploaded_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    # Read off a store invoice PDF (app/store_invoices.py): the store's invoice number, and a JSON copy of its
    # lines, totals and open cores. Deferred, so pages work the same before 20261128_parts_receipt_lines.sql runs.
    # FetchedValue: an INSERT leaves them out unless set, so saving works on a table without them.
    invoice_number = deferred(Column(String(32), index=True, server_default=FetchedValue()))
    details = deferred(Column(Text, server_default=FetchedValue()))
    __mapper_args__ = {"eager_defaults": False}  # never read them back on INSERT (RETURNING) either


_ready: dict = {}  # per database engine: (checked at, table exists)
_lines_ready: dict = {}  # per database engine: (checked at, line columns exist)


def ready(db: Session) -> bool:
    """True once the receipts table exists. A missing table is checked again a minute later, not on every page."""
    bind = db.get_bind()
    at, ok = _ready.get(id(bind), (0.0, False))
    if ok or (at and time.monotonic() - at < 60):
        return ok
    try:
        ok = inspect(bind).has_table("parts_receipts")
    except SQLAlchemyError:
        ok = False
    _ready[id(bind)] = (time.monotonic(), ok)
    return ok


def lines_ready(db: Session) -> bool:
    """True once the receipts table has the columns for invoice lines (20261128_parts_receipt_lines.sql)."""
    bind = db.get_bind()
    at, ok = _lines_ready.get(id(bind), (0.0, False))
    if ok or (at and time.monotonic() - at < 60):
        return ok
    try:
        ok = ready(db) and {"invoice_number", "details"} <= {c["name"] for c in inspect(bind).get_columns("parts_receipts")}
    except SQLAlchemyError:
        ok = False
    _lines_ready[id(bind)] = (time.monotonic(), ok)
    return ok


def can_upload(role: str | None) -> bool:
    return role in STAFF


def can_view(role: str | None) -> bool:
    """Receipts show what the shop paid, so they stay with the office, as part costs do on the Parts card."""
    return role in OFFICE


def sniff(head: bytes) -> str | None:
    from app.routes.ro_evidence import sniff as _sniff
    return _sniff(head)


# ---------------------------------------------------------------- reading a PO off a PDF

_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.DOTALL)
_TEXT_OP = re.compile(rb"\[((?:[^\]\\]|\\.)*)\]\s*TJ|\(((?:[^()\\]|\\.)*)\)\s*(?:Tj|'|\")", re.DOTALL)
_LITERAL = re.compile(rb"\(((?:[^()\\]|\\.)*)\)", re.DOTALL)
_MAX_INFLATED = 20 * 1024 * 1024


def _unescape(raw: bytes) -> str:
    raw = re.sub(rb"\\([nrtbf()\\])", lambda m: {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"", b"f": b""}.get(m.group(1), m.group(1)), raw)
    raw = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), raw)
    return raw.decode("latin-1", "replace")


def _streams(data: bytes):
    """Each content stream, inflated when it is Flate-compressed (as most store PDFs are)."""
    budget = _MAX_INFLATED
    for match in _STREAM.finditer(data):
        body = match.group(1).rstrip(b"\r\n")
        if body.endswith(b"~>"):  # ASCII85 on top of Flate, as some PDF writers do
            try:
                body = base64.a85decode(body, adobe=True)
            except ValueError:
                pass
        try:
            inflater = zlib.decompressobj()
            out = inflater.decompress(body, max(budget, 0))
            budget -= len(out)
            yield out
        except zlib.error:
            yield body
        if budget <= 0:
            return


def pdf_text(data: bytes) -> str:
    """Best-effort text of a PDF: the strings drawn on its pages. Pieces of one string run together, so a PO
    the PDF draws in parts ("IAC-PO-" then "0021") still reads whole. Fonts that encode text as glyph
    numbers can't be read this way; the PO is then typed in."""
    if not data.startswith(b"%PDF-"):
        return ""
    lines = []
    for stream in _streams(data):
        for match in _TEXT_OP.finditer(stream):
            if match.group(1) is not None:
                lines.append("".join(_unescape(m.group(1)) for m in _LITERAL.finditer(match.group(1))))
            else:
                lines.append(_unescape(match.group(2)))
    return "\n".join(lines)


def po_jobs_on(data: bytes, content_type: str, filename: str = "") -> list[int]:
    """Job numbers of the shop's POs printed on the receipt (PDFs only) or in its file name."""
    found = po_numbers.find_in_text(filename)
    if content_type == "application/pdf":
        for job in po_numbers.find_in_text(pdf_text(data)):
            if job not in found:
                found.append(job)
    return found


# ---------------------------------------------------------------- saving and listing

def clean_total(value) -> float | None:
    text = str(value or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        amount = round(float(text), 2)
    except ValueError:
        raise ValueError("Enter the receipt total as a number, like 84.99.")
    if amount < 0 or amount > 1_000_000:
        raise ValueError("Enter the receipt total as a number, like 84.99.")
    return amount


def _short(text, limit: int) -> str | None:
    text = " ".join(str(text or "").split())
    return text[:limit] or None


def make(ro_id: int | None, data: bytes, content_type: str, filename: str, uploaded_by: str, *, vendor=None, total=None,
         po_text=None, note=None, matched_by: str = "job_page", invoice: dict | None = None) -> PartsReceipt:
    row = PartsReceipt(repair_order_id=ro_id, vendor=_short(vendor, 128), total=total, po_text=_short(po_text, 64),
                       note=_short(note, 500), matched_by=matched_by if matched_by in HOW else "job_page",
                       filename=(filename or "receipt").rsplit("/", 1)[-1][:200] or "receipt",
                       content_type=content_type, size=len(data), data=data, uploaded_by=(uploaded_by or "staff")[:64])
    if invoice is not None:  # only when lines_ready(); otherwise the columns aren't there to write
        row.invoice_number = invoice.get("invoice_number")
        row.details = json.dumps(invoice, separators=(",", ":"))
    return row


_META = ("id", "repair_order_id", "vendor", "po_text", "total", "note", "matched_by", "filename", "content_type",
         "size", "uploaded_by", "created_at")


def _listing(db: Session):
    """A receipts query that loads everything but the file bytes, and the invoice lines once they exist."""
    names = _META + (("invoice_number", "details") if lines_ready(db) else ())
    return db.query(PartsReceipt).options(load_only(*(getattr(PartsReceipt, n) for n in names)))


def for_job(db: Session, ro_id: int) -> list[PartsReceipt]:
    """Metadata only; the file bytes are never loaded for a page."""
    if not ready(db):
        return []
    return (_listing(db).filter(PartsReceipt.repair_order_id == ro_id)
            .order_by(PartsReceipt.created_at.asc(), PartsReceipt.id.asc()).all())


def unmatched(db: Session) -> list[PartsReceipt]:
    """Store invoices saved without a job, newest first."""
    if not lines_ready(db):
        return []
    return (_listing(db).filter(PartsReceipt.repair_order_id.is_(None))
            .order_by(PartsReceipt.created_at.desc(), PartsReceipt.id.desc()).limit(200).all())


def same_invoice(db: Session, number: str | None) -> PartsReceipt | None:
    """A receipt already saved for this store invoice number, wherever it is."""
    if not number or not lines_ready(db):
        return None
    return _listing(db).filter(PartsReceipt.invoice_number == number).order_by(PartsReceipt.id).first()


_parsed: dict = {}  # (receipt id, size) -> lines read from a PDF saved before the line columns existed


def invoice_of(db: Session, row: PartsReceipt) -> dict | None:
    """The store invoice's lines for a receipt: saved with it, or read from its PDF once and kept in memory."""
    from app import store_invoices
    if lines_ready(db):
        raw = row.details
        if raw:
            try:
                return json.loads(raw)
            except ValueError:
                return None
    if row.content_type != "application/pdf":
        return None
    key = (row.id, row.size)
    if key not in _parsed:
        data = db.query(PartsReceipt.data).filter(PartsReceipt.id == row.id).scalar()
        _parsed[key] = store_invoices.parse(data or b"", row.content_type)
        if len(_parsed) > 500:
            _parsed.pop(next(iter(_parsed)))
    return _parsed[key]


def out_of_area(invoice: dict | None) -> bool:
    """A store in another state than the shop, worth a second look (the account can be used anywhere)."""
    from app.estimate_pdf import SHOP_ADDRESS
    home = re.search(r",\s*([A-Z]{2})\s+\d{5}", SHOP_ADDRESS)
    state = (invoice or {}).get("store_state")
    return bool(home and state and state != home.group(1))


def card(db: Session, ro, role: str | None) -> dict:
    """What the job page's Parts receipts card needs."""
    from app import store_invoices
    on = ready(db)
    rows = for_job(db, ro.id) if on else []
    view = can_view(role)
    invoices, checks, flags = {}, {}, []
    if view and rows:
        items, owners = [], []
        for r in rows:
            inv = invoice_of(db, r)
            if inv and inv.get("items"):
                invoices[r.id] = inv
                for item in inv["items"]:
                    items.append(item)
                    owners.append(r.id)
        if items:
            lines = sorted(ro.line_items, key=lambda line: line.id or 0)
            for owner, entry in zip(owners, store_invoices.compare(items, lines)):
                checks.setdefault(owner, []).append(entry)
                if entry["status"] in ("not_on_job", "under_cost", "not_approved"):
                    flags.append(entry)
    part_lines = [line for line in ro.line_items if (line.item_type or "") == "part" and line.billable]
    return {
        "ready": on,
        "lines_on": on and lines_ready(db),
        "po": ro.po_number,
        "rows": rows if view else [],
        "count": len(rows),
        "total": round(sum(r.total or 0 for r in rows), 2) if view else None,
        "can_upload": on and can_upload(role),
        "can_view": view,
        "how": HOW,
        "invoices": invoices,
        "checks": checks,
        "flags": flags,
        "status": store_invoices.STATUS,
        "billed_parts": round(sum(line.total for line in part_lines), 2) if view else None,
        "out_of_area": {rid: out_of_area(inv) for rid, inv in invoices.items()},
    }
