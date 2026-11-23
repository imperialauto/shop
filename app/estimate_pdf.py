"""A job's estimate as numbers (totals) and as a printable PDF.

Read only: nothing here writes to the database or contacts anyone. Used by the job page's
Print estimate button (/ro/{id}/estimate.pdf) and the assistant's estimate tools.

Lines follow the dashboard's own conventions (notes/estimate-process.md):
* one line per part, labor and shop supplies;
* $0 misc lines for diagnostic findings and the diagnostic credit;
* add-ons are lines whose description starts with "OPTIONAL", shown in their own section;
* declined lines are listed but never counted.
Tax is the dashboard's parts-only rate, the same one invoices and deposit texts use.
"""
import io
from datetime import date, timedelta
from xml.sax.saxutils import escape

from app.models import RepairOrder

SHOP_NAME = "Imperial Auto Care LLC"
SHOP_ADDRESS = "7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307"
SHOP_CONTACT = "480-914-4144 · imperialautophx.com"
VALID_DAYS = 14
DEPOSIT_SHARE = 0.5
PURPLE = "#2D0245"
GOLD = "#CBB06B"
TERMS = (
    f"This estimate is valid for {VALID_DAYS} days from the date above.",
    "A 50% deposit is required before work begins.",
    "Additional work found during repair will be quoted before it is performed.",
    "Storage fees begin 3 days after notice that the vehicle is complete.",
    "Payment accepted: Zelle or cash. Snap Finance is available if you would like to make payments.",
)


def tax_rate() -> float:
    from app.routes.invoices import SHOP_TAX_RATE  # late import: invoices imports the app's routers
    return SHOP_TAX_RATE


def is_optional(line) -> bool:
    return (line.description or "").lstrip().upper().startswith("OPTIONAL")


def _sums(lines) -> dict:
    parts = sum(li.total for li in lines if li.item_type == "part")
    labor = sum(li.total for li in lines if li.item_type == "labor")
    other = sum(li.total for li in lines if li.item_type not in ("part", "labor"))
    tax = round(parts * tax_rate(), 2)
    return {"parts": round(parts, 2), "labor": round(labor, 2),
            "labor_hours": round(sum(li.quantity for li in lines if li.item_type == "labor"), 2),
            "other": round(other, 2), "tax": tax, "total": round(parts + labor + other + tax, 2),
            "lines": len(lines)}


def totals(ro: RepairOrder) -> dict:
    """Recommended work, optional add-ons and the grand total. Declined lines are left out."""
    counted = [li for li in ro.line_items if li.approval != "declined"]
    main = _sums([li for li in counted if not is_optional(li)])
    optional = _sums([li for li in counted if is_optional(li)])
    return {"tax_rate": tax_rate(), "recommended": main, "optional": optional,
            "total_with_options": round(main["total"] + optional["total"], 2),
            "deposit": round(main["total"] * DEPOSIT_SHARE, 2),
            "declined": sum(1 for li in ro.line_items if li.approval == "declined")}


def filename(ro: RepairOrder) -> str:
    c = ro.customer
    last = "".join(ch for ch in ((c.last_name or c.first_name or "") if c else "") if ch.isalnum())
    return f"Estimate_RO{ro.id}" + (f"_{last}" if last else "") + ".pdf"


def _money(value: float) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def _qty(line) -> str:
    q = line.quantity or 0
    text = f"{q:g}"
    if line.item_type == "labor":
        return f"{text} hr" if q == 1 else f"{text} hrs"
    return text


def build(ro: RepairOrder, today: date | None = None) -> bytes:
    """The estimate as a one- or two-page letter PDF, in the shop's purple and gold."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    today = today or date.today()
    purple, gold = colors.HexColor(PURPLE), colors.HexColor(GOLD)
    grey, shade = colors.HexColor("#57534e"), colors.HexColor("#f5f0ea")
    base = getSampleStyleSheet()["Normal"]
    s = {
        "shop": ParagraphStyle("shop", parent=base, fontName="Helvetica-Bold", fontSize=15, leading=18, textColor=purple),
        "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=11, textColor=grey),
        "title": ParagraphStyle("title", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22,
                                textColor=purple, alignment=TA_RIGHT),
        "right": ParagraphStyle("right", parent=base, fontSize=8.5, leading=11, textColor=grey, alignment=TA_RIGHT),
        "label": ParagraphStyle("label", parent=base, fontName="Helvetica-Bold", fontSize=8, leading=10,
                                textColor=gold, spaceAfter=2),
        "body": ParagraphStyle("body", parent=base, fontSize=9.5, leading=12.5, textColor=colors.black),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8.5, leading=11),
        "struck": ParagraphStyle("struck", parent=base, fontSize=8.5, leading=11, textColor=grey),
    }

    def para(text, style="body"):
        return Paragraph(escape(str(text or "")).replace("\r\n", "\n").replace("\n", "<br/>"), s[style])

    width = 7 * inch
    story = []
    head = Table([[[para(SHOP_NAME, "shop"), para(SHOP_ADDRESS, "small"), para(SHOP_CONTACT, "small")],
                   [Paragraph("ESTIMATE", s["title"]), para(f"Repair order #{ro.id}", "right"),
                    para(f"Date: {today:%B %d, %Y} · Valid until {today + timedelta(days=VALID_DAYS):%B %d, %Y}",
                         "right")]]],
                 colWidths=[width * 0.55, width * 0.45])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ("LINEBELOW", (0, 0), (-1, 0), 1.5, gold), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [head, Spacer(1, 10)]

    c, v = ro.customer, ro.vehicle
    who = [para(" ".join(p for p in ((c.first_name, c.last_name) if c else ()) if p) or "Customer")]
    if c and c.phone:
        who.append(para(c.phone))
    car = [para(" ".join(str(p) for p in (v.year, v.make, v.model, v.trim) if p) if v else "Vehicle")]
    if v:
        detail = " · ".join(p for p in (v.engine, f"VIN {v.vin}" if v.vin else None,
                                        f"Plate {v.license_plate}" if v.license_plate else None) if p)
        if detail:
            car.append(para(detail, "small"))
    if ro.mileage_in:
        car.append(para(f"Mileage in: {ro.mileage_in:,}", "small"))
    info = Table([[para("CUSTOMER", "label"), para("VEHICLE", "label")], [who, car]],
                 colWidths=[width * 0.5, width * 0.5])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [info, Spacer(1, 8)]
    if ro.concern:
        story += [para("CUSTOMER CONCERN", "label"), para(ro.concern), Spacer(1, 8)]

    sums = totals(ro)
    counted = [li for li in ro.line_items if li.approval != "declined"]
    notes = [li for li in counted if not is_optional(li) and li.item_type == "misc" and not li.unit_price]
    main = [li for li in counted if not is_optional(li) and li not in notes]
    optional = [li for li in counted if is_optional(li)]
    declined = [li for li in ro.line_items if li.approval == "declined"]

    for li in notes:  # $0 note lines: findings, diagnostic credit
        story += [para(li.description), Spacer(1, 4)]

    def item_table(lines, money_rows, strike=False):
        rows = [["Description", "Qty", "Unit", "Amount"]]
        for li in lines:
            rows.append([para(li.description, "struck" if strike else "cell"), _qty(li), _money(li.unit_price),
                         "declined" if strike else _money(li.total)])
        first_total = len(rows)
        rows += money_rows
        t = Table(rows, colWidths=[width * 0.6, width * 0.12, width * 0.13, width * 0.15], repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), purple), ("TEXTCOLOR", (0, 0), (-1, 0), gold),
                 ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                 ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        if len(lines):
            style.append(("ROWBACKGROUNDS", (0, 1), (-1, first_total - 1), [shade, colors.white]))
        if money_rows:
            style += [("LINEABOVE", (0, first_total), (-1, first_total), 0.75, gold),
                      ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]
            for r in range(first_total, len(rows)):
                style.append(("SPAN", (0, r), (2, r)))
                style.append(("ALIGN", (0, r), (0, r), "RIGHT"))
        t.setStyle(TableStyle(style))
        return t

    def money_rows(part, label):
        rows = []
        if part["parts"]:
            rows.append(["Parts subtotal", "", "", _money(part["parts"])])
        if part["labor"]:
            rows.append([f"Labor subtotal ({part['labor_hours']:g} hrs)", "", "", _money(part["labor"])])
        if part["other"]:
            rows.append(["Shop supplies and other", "", "", _money(part["other"])])
        if part["tax"]:
            rows.append([f"Tax {sums['tax_rate'] * 100:g}% (parts only)", "", "", _money(part["tax"])])
        rows.append([label, "", "", _money(part["total"])])
        return rows

    story += [para("RECOMMENDED REPAIR", "label"),
              item_table(main, money_rows(sums["recommended"], "RECOMMENDED REPAIR TOTAL")), Spacer(1, 10)]
    if optional:
        grand = Table([["TOTAL WITH ALL ADD-ONS", _money(sums["total_with_options"])]],
                      colWidths=[width * 0.85, width * 0.15])
        grand.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT"), ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                                   ("FONTSIZE", (0, 0), (-1, -1), 9), ("LINEABOVE", (0, 0), (-1, 0), 1.5, purple)]))
        story += [para("OPTIONAL ADD-ONS (approve or decline each one)", "label"),
                  item_table(optional, money_rows(sums["optional"], "OPTIONAL ADD-ONS TOTAL")), Spacer(1, 4),
                  grand, Spacer(1, 10)]
    if declined:
        story += [para("DECLINED", "label"), item_table(declined, [], strike=True), Spacer(1, 10)]

    terms = [para("TERMS", "label")]
    for line in TERMS:
        if line.startswith("A 50% deposit") and sums["deposit"] > 0:
            line = f"A 50% deposit is required before work begins: {_money(sums['deposit'])} for the recommended repair."
        terms.append(para(line, "small"))
    terms += [Spacer(1, 10), para("Thank you for choosing Imperial Auto Care", "small")]
    story.append(KeepTogether(terms))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            title=f"Estimate, repair order #{ro.id}", author=SHOP_NAME)
    doc.build(story)
    return buf.getvalue()
