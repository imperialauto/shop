"""Made-up AutoZone Pro invoice PDFs, drawn the way AutoZone's own PDFs are (one Flate content stream a page,
every string placed with `q 0.000 g BT x y Td (text) Tj ET Q`, columns at the same x positions, a logo image
stream first). Nothing here is a real invoice, store account, customer or card."""
import zlib

HEADER = [
    (45, 730, "Commercial Invoice"), (486, 757, "AutoZone Store 09999"), (486, 749, "100 W TEST RD"),
    (499, 740, "Phoenix, AZ 85000"), (518, 731, "602.555.0100"), (45, 712, "Deliver To"), (45, 695, " SAMPLE SHOP"),
    (51, 655, " Phone"), (164, 655, ": (602) 555-0199"), (51, 644, " Customer #"), (164, 644, ": 10000001"),
    (300, 712, "Order Information"), (300, 697, "Invoice Number"), (414, 697, ": {invoice}"),
    (300, 686, "PO Number"), (414, 686, ": {po}"), (300, 675, "Comm Specialist"), (414, 675, ": "),
    (420, 675, "COMMERCIAL SALES"), (420, 663, "CENTER"), (300, 652, "Register Number"), (414, 652, ": 35"),
    (300, 641, "Order Date"), (414, 641, ": 11/25/2026 01:07 PM"), (45, 623, "Items"),
    (71, 605, "Part #"), (118, 605, "QTY"), (234, 605, "Description"), (393, 605, "List"), (438, 605, "Cost"),
    (484, 605, "Core"), (531, 605, "Total"),
]

# Parts as AutoZone prints them: a part row, then a row for the wrapped SKU, a second part number, or a deal.
ITEMS = [
    (174, 592, "2005 Cadillac Truck Escalade 2WD"),
    (58, 580, "TS34-31351B"), (124, 580, "2"), (138, 580, "SHOCK ABSOR"), (197, 580, " Duralast Twintube Shock"),
    (288, 580, "  SKU-000937555"), (400, 580, "39.31"), (447, 580, "18.00"), (499, 580, "0.00"), (541, 580, "36.00"),
    (57, 567, "MS34-31422B"), (124, 567, "2"), (138, 567, "MONOTUBE S"), (192, 567, " Duralast Monotube Shock"),
    (286, 567, "  SKU-000087143"), (400, 567, "69.86"), (447, 567, "39.33"), (499, 567, "0.00"), (541, 567, "78.66"),
    (162, 553, "No vehicle given for the following items"),
    (62, 541, "SZND536G"), (124, 541, "1"), (138, 541, "STP 5W-30 DEXOS"), (210, 541, " STP 5W-30 Dexos Motor Oil"),
    (396, 541, "317.44"), (443, 541, "149.99"), (499, 541, "0.00"), (537, 541, "149.99"),
    (56, 528, "SZND536GND"), (138, 528, "SKU-000562203"),
    (138, 515, "DEAL: "), (164, 515, "STP FS Box"), (208, 515, "  You saved $50.00"),
    (71, 502, "82180"), (124, 502, "1"), (138, 502, "ULTRA   BLACK GAS"),
    (217, 502, " Permatex Ultra Black RTV Silicone Gasket"), (400, 502, "21.72"), (447, 502, "11.99"),
    (499, 502, "0.00"), (541, 502, "11.99"),
    (71, 490, "82181"), (138, 490, "Maker"), (161, 490, "  SKU-000554295"),
    (61, 477, "9-0738SWP"), (124, 477, "1"), (138, 477, "ENGINE TIMING"), (199, 477, " Cloyes Engine Timing Chain Kit"),
    (396, 477, "693.14"), (443, 477, "400.00"), (499, 477, "0.00"), (537, 477, "400.00"),
    (138, 464, "SKU-001173123"),
    (80, 451, "78DT"), (124, 451, "1"), (138, 451, "BATTERY"), (190, 451, " Duralast Gold Battery"),
    (286, 451, "  SKU-000111222"), (396, 451, "249.99"), (443, 451, "170.00"), (499, 451, "22.00"),
    (537, 451, "192.00"),
]

FOOTER = [
    (45, 391, "Core Bank"), (129, 392, "(Not reflected in invoice total)"), (339, 391, "Total Core Bank:    $44.00"),
    (78, 373, "Part #"), (136, 373, "Cores Older Than 3 Days"), (247, 373, "Invoice #"), (304, 373, "Core ID"),
    (364, 373, "Date"), (417, 373, "Core"),
    (80, 361, "GC2"), (132, 361, "GOLF CART BATTERY 6"), (240, 361, "09999000001"), (307, 361, "18949"),
    (352, 361, "2026-09-25"), (416, 361, "22.00"),
    (80, 348, "78-T"), (132, 348, "TOTAL PRO BATTERY"), (240, 348, "09999000002"), (307, 348, "11019"),
    (352, 348, "2026-10-08"), (416, 348, "22.00"),
    (305, 332, "Outstanding Cores over 3 days:    $44.00"),
    (242, 139, "MSDS can be ordered upon request"),
    (429, 106, "Subtotal"), (531, 106, "${total}"), (429, 92, "Tax"), (542, 92, "$0.00"),
    (225, 79, "VISA  ****0000"), (347, 79, "000000"), (394, 79, "${total}"), (429, 78, "Total Due"),
    (531, 78, "${total}"), (228, 57, "AZC Savings"), (314, 57, "Piece Count"), (410, 57, "Page"), (503, 57, "Total"),
    (240, 35, "$88.56"), (344, 35, "8"), (402, 35, "1 of 1"), (489, 35, "${total}"),
    (45, 16, "cmstinvc_EN_US_3.0.7.py"),
]


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def content(cells) -> bytes:
    out = ["2 J", "0.71 w", "q 170.08 0 0 17.89 42.52 745.76 cm /I1 Do Q", "BT /F1 8.00 Tf ET"]
    for x, y, text in cells:
        out.append(f"q 0.000 g BT {x:.2f} {y:.2f} Td ({_esc(text)}) Tj ET Q")
    return "\n".join(out).encode("latin-1")


def pdf(invoice="09999000123", po="IAC-PO-0021", total="868.64", items=None, pages=None) -> bytes:
    """A whole invoice PDF. `pages` replaces the cells entirely (a list of cell lists, one per page)."""
    if pages is None:
        fill = lambda cells: [(x, y, t.replace("{invoice}", invoice).replace("{po}", po).replace("{total}", total))
                              for x, y, t in cells]
        pages = [fill(HEADER) + (ITEMS if items is None else items) + fill(FOOTER)]
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    logo = zlib.compress(b"\x00\xff" * 400)
    objects.append(b"<< /Type /XObject /Subtype /Image /Width 20 /Height 20 /BitsPerComponent 8 /ColorSpace /DeviceGray "
                   b"/Filter /FlateDecode /Length " + str(len(logo)).encode() + b" >>\nstream\n" + logo + b"\nendstream")
    for cells in pages:
        body = zlib.compress(content(cells))
        objects.append(b"<< /Filter /FlateDecode /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream")
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
                       + str(len(objects)).encode() + b" 0 R /Resources << /XObject << /I1 3 0 R >> >> >>")
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [" + b" ".join(f"{k} 0 R".encode() for k in kids) + b"] /Count " \
        + str(len(kids)).encode() + b" >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
