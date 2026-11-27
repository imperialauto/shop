"""Reading the lines off a parts store's PDF invoice, and holding them against what a job bills.

Today this reads AutoZone Pro (commercial) invoices: the PDF that Transaction History > View or Download gives.
It finds the invoice number, the PO typed at the counter, the order date, the store, each part bought (part
number, quantity, list, cost, core, line total), the subtotal, tax and total, and the cores the store says are
still owed. It never reads or keeps how the invoice was paid.

The PDF draws each piece of text at a position, so the reader puts the pieces back into rows by position and
reads the columns from the invoice's own "Part # / QTY / Description / List / Cost / Core / Total" header. Any
PDF that isn't an AutoZone invoice gives None, and the receipt is kept as a plain file as before.

compare() holds the parts bought against the job's part lines and says, per part, which job line looks like
it bills it, and flags parts that look unbilled or billed under what the store charged. It is a best guess
(job lines carry no part number field, only a description), so the page says "looks like" and never changes
a line, a price or anything a customer sees.
"""
import re
import zlib

from app import parts_receipts

# ---------------------------------------------------------------- positioned text

_TOKEN = re.compile(
    rb"\((?:[^()\\]|\\.)*\)"          # literal string (AutoZone escapes inner parentheses)
    rb"|<[0-9A-Fa-f\s]*>"              # hex string
    rb"|\[(?:[^\]\\]|\\.)*\]"          # array (TJ)
    rb"|/[^\s/\[\]()<>{}%]+"           # name
    rb"|[+-]?(?:\d+\.?\d*|\.\d+)"      # number
    rb"|[A-Za-z'\"*]+[0-9]?\*?"        # operator
    rb"|<<|>>|%[^\r\n]*",
    re.DOTALL)
_LITERAL = re.compile(rb"\(((?:[^()\\]|\\.)*)\)", re.DOTALL)


def _string(token: bytes) -> str:
    if token.startswith(b"("):
        return parts_receipts._unescape(token[1:-1])
    if token.startswith(b"<"):
        hexs = re.sub(rb"\s", b"", token[1:-1])
        if len(hexs) % 2:
            hexs += b"0"
        try:
            return bytes.fromhex(hexs.decode()).decode("latin-1", "replace")
        except ValueError:
            return ""
    if token.startswith(b"["):
        return "".join(parts_receipts._unescape(m.group(1)) for m in _LITERAL.finditer(token[1:-1]))
    return ""


def _mul(a, b):
    """Product of two PDF matrices [a b c d e f]."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _cells(stream: bytes) -> list[tuple[float, float, str]]:
    """(x, y, text) of each string a content stream draws, in page space."""
    out = []
    ctm, stack = [1, 0, 0, 1, 0, 0], []
    tm = lm = [1, 0, 0, 1, 0, 0]
    leading = 0.0
    args = []

    def show(text):
        if text.strip():
            m = _mul(tm, ctm)
            out.append((round(m[4], 2), round(m[5], 2), text))

    for match in _TOKEN.finditer(stream):
        tok = match.group(0)
        first = tok[:1]
        if first in b"([<" and tok not in (b"<<",):
            args.append(tok)
            continue
        if first in b"+-.0123456789":
            try:
                args.append(float(tok))
            except ValueError:
                args = []
            continue
        if first in b"/%" or tok in (b"<<", b">>"):
            args.append(tok)
            continue
        op = tok
        nums = [a for a in args if isinstance(a, float)]
        if op == b"q":
            stack.append(ctm)
        elif op == b"Q":
            ctm = stack.pop() if stack else [1, 0, 0, 1, 0, 0]
        elif op == b"cm" and len(nums) >= 6:
            ctm = _mul(nums[-6:], ctm)
        elif op == b"BT":
            tm = lm = [1, 0, 0, 1, 0, 0]
        elif op == b"Tm" and len(nums) >= 6:
            tm = lm = nums[-6:]
        elif op in (b"Td", b"TD") and len(nums) >= 2:
            tx, ty = nums[-2:]
            if op == b"TD":
                leading = -ty
            tm = lm = _mul([1, 0, 0, 1, tx, ty], lm)
        elif op == b"TL" and nums:
            leading = nums[-1]
        elif op == b"T*":
            tm = lm = _mul([1, 0, 0, 1, 0, -leading], lm)
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            if op in (b"'", b'"'):
                tm = lm = _mul([1, 0, 0, 1, 0, -leading], lm)
            strings = [a for a in args if isinstance(a, bytes) and a[:1] in b"([<"]
            if strings:
                show(_string(strings[-1]))
        args = []
    return out


def pages(data: bytes) -> list[list[tuple[float, float, str]]]:
    """Positioned text per content stream that draws text (one per page for store invoices)."""
    if not data.startswith(b"%PDF-"):
        return []
    found = []
    for stream in parts_receipts._streams(data):
        if b"BT" not in stream or (b"Tj" not in stream and b"TJ" not in stream):
            continue
        cells = _cells(stream)
        if cells:
            found.append(cells)
    return found


def rows(cells, tolerance: float = 1.6) -> list[list[tuple[float, str]]]:
    """Cells grouped into rows top to bottom, each row left to right, as (x, text)."""
    grouped = []
    for x, y, text in sorted(cells, key=lambda c: (-c[1], c[0])):
        if grouped and abs(grouped[-1][0] - y) <= tolerance:
            grouped[-1][1].append((x, text))
        else:
            grouped.append([y, [(x, text)]])
    return [sorted(r, key=lambda c: c[0]) for _, r in grouped]


# ---------------------------------------------------------------- AutoZone invoices

_MONEY = re.compile(r"^\(?-?\$?\s*-?[\d,]*\.\d{2}\)?$")
_QTY = re.compile(r"^\d{1,4}(?:\.\d+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STOP = ("Core Bank", "Subtotal", "MSDS", "Outstanding Cores", "AZC Savings", "Cores Older")


def money(text) -> float | None:
    text = str(text or "").strip()
    if not _MONEY.match(text):
        return None
    negative = text.startswith("(") or "-" in text
    value = float(re.sub(r"[^\d.]", "", text) or 0)
    return round(-value if negative else value, 2)


def _clean(text: str) -> str:
    return " ".join(text.split())


def _labelled(all_rows, label: str) -> str | None:
    """The value printed after `label` and a colon, e.g. "PO Number" then ": 05 CADILLAC"."""
    for row in all_rows:
        for i, (_, text) in enumerate(row):
            if _clean(text).lower() == label.lower():
                rest = " ".join(t for _, t in row[i + 1:]).strip()
                return _clean(rest.lstrip(":").strip()) or None
            if _clean(text).lower().startswith(label.lower() + " :") or _clean(text).lower().startswith(label.lower() + ":"):
                return _clean(text.split(":", 1)[1]) or None
    return None


def _amount_after(all_rows, label: str) -> float | None:
    for row in all_rows:
        for i, (x, text) in enumerate(row):
            if _clean(text).lower() == label.lower():
                for _, t in row[i + 1:]:
                    value = money(t)
                    if value is not None:
                        return value
    return None


def _columns(header) -> dict | None:
    names = {"part #": "part", "part#": "part", "qty": "qty", "description": "desc", "list": "list",
             "cost": "cost", "core": "core", "total": "total"}
    cols = {}
    for x, text in header:
        key = names.get(_clean(text).lower())
        if key:
            cols[key] = x
    return cols if {"part", "qty", "cost", "total"} <= cols.keys() else None


def _nearest(x, cols, keys):
    return min(keys, key=lambda k: abs(cols[k] - x))


def _items(page_rows) -> list[dict]:
    items = []
    cols = None
    current = None
    for row in page_rows:
        joined = " ".join(t for _, t in row)
        if cols is None:
            cols = _columns(row)
            continue
        if any(joined.strip().startswith(s) or s in joined for s in _STOP):
            break
        money_keys = [k for k in ("list", "cost", "core", "total") if k in cols]
        money_left = min(cols[k] for k in money_keys) - 25
        part_right = cols["qty"] - 8
        qty_cells = [t for x, t in row if abs(x - cols["qty"]) <= 12 and _QTY.match(t.strip())]
        amounts = [(x, money(t)) for x, t in row if x >= money_left and money(t) is not None]
        if qty_cells and len(amounts) >= 2:
            values = {}
            for x, value in amounts:
                values.setdefault(_nearest(x, cols, money_keys), value)
            current = {
                "parts": [_clean(t) for x, t in row if x < part_right and _clean(t)],
                "qty": float(qty_cells[0]),
                "name": _clean(" ".join(t for x, t in row if part_right + 20 <= x < money_left and not t.strip().startswith("SKU-"))),
                "sku": next((_clean(t)[4:] for x, t in row if t.strip().startswith("SKU-")), None),
                "list": values.get("list"), "cost": values.get("cost"), "core": values.get("core") or 0.0,
                "total": values.get("total"), "deal": None,
            }
            if current["qty"] == int(current["qty"]):
                current["qty"] = int(current["qty"])
            items.append(current)
            continue
        text = _clean(joined)
        if not text:
            continue
        if text.startswith("No vehicle given") or (re.match(r"^(19|20)\d{2} [A-Z]", text)
                                                    and all(x >= part_right for x, _ in row)):
            current = None  # a vehicle heading starts the next group of parts
            continue
        if current is None:
            continue
        for x, t in row:
            t = _clean(t)
            if not t:
                continue
            if x < part_right:
                if t not in current["parts"]:
                    current["parts"].append(t)
            elif t.startswith("SKU-"):
                current["sku"] = current["sku"] or t[4:]
            elif t.startswith("DEAL:") or current["deal"] is not None:
                current["deal"] = _clean(f"{current['deal'] or ''} {t}")
            elif x < money_left:
                current["name"] = _clean(f"{current['name']} {t}")
    for item in items:
        if item["deal"]:
            item["deal"] = _clean(item["deal"].replace("DEAL:", ""))[:120] or None
        item["name"] = item["name"][:160]
        item["parts"] = item["parts"][:4]
    return items


def _cores(all_rows) -> list[dict]:
    cores = []
    started = False
    for row in all_rows:
        joined = " ".join(t for _, t in row)
        if "Cores Older" in joined:
            started = True
            continue
        if not started:
            continue
        if "Outstanding Cores" in joined or "Subtotal" in joined or "MSDS" in joined:
            break
        texts = [_clean(t) for _, t in row if _clean(t)]
        dates = [t for t in texts if _DATE.match(t)]
        amount = money(texts[-1]) if texts else None
        if dates and amount is not None and len(texts) >= 4:
            cores.append({"part": texts[0], "name": texts[1] if len(texts) > 5 else "", "invoice": texts[2] if len(texts) > 5 else "",
                          "date": dates[0], "amount": amount})
    return cores


def parse_autozone(data: bytes) -> dict | None:
    """The invoice's fields and lines, or None if this isn't an AutoZone invoice PDF."""
    found = pages(data)
    if not found:
        return None
    page_rows = [rows(cells) for cells in found]
    all_rows = [r for page in page_rows for r in page]
    words = " ".join(t for r in all_rows for _, t in r)
    if "autozone" not in words.lower() or "Invoice Number" not in words:
        return None
    items = []
    for page in page_rows:
        items.extend(_items(page))
    store = next((_clean(t) for r in all_rows for _, t in r if _clean(t).lower().startswith("autozone store")), None)
    store_city = None
    for i, r in enumerate(all_rows):
        if any(_clean(t).lower().startswith("autozone store") for _, t in r):
            for later in all_rows[i + 1:i + 4]:
                hit = next((_clean(t) for _, t in later if re.search(r",\s*[A-Z]{2}\s+\d{5}", t)), None)
                if hit:
                    store_city = hit
                    break
            break
    owed = re.search(r"Outstanding Cores over \d+ days:\s*\$?([\d,]+\.\d{2})", words)
    total = _amount_after(all_rows, "Total Due")
    return {
        "kind": "autozone",
        "store": store.replace("Store ", "") if store else "AutoZone",
        "store_city": store_city,
        "store_state": (re.search(r",\s*([A-Z]{2})\s+\d{5}", store_city).group(1) if store_city else None),
        "invoice_number": (_labelled(all_rows, "Invoice Number") or "")[:32] or None,
        "po": (_labelled(all_rows, "PO Number") or "")[:64] or None,
        "order_date": (_labelled(all_rows, "Order Date") or "")[:32] or None,
        "items": items,
        "subtotal": _amount_after(all_rows, "Subtotal"),
        "tax": _amount_after(all_rows, "Tax"),
        "total": total,
        "cores_open": _cores(all_rows),
        "cores_open_total": float(owed.group(1).replace(",", "")) if owed else None,
    }


def parse(data: bytes, content_type: str) -> dict | None:
    """Fields and lines of a store invoice PDF we know how to read, or None. Never raises."""
    if content_type != "application/pdf":
        return None
    try:
        return parse_autozone(data)
    except (ValueError, IndexError, KeyError, TypeError, zlib.error, AttributeError):
        return None


# ---------------------------------------------------------------- bought against billed

_WORD = re.compile(r"[A-Za-z]{4,}")
_PLAIN = re.compile(r"[^A-Z0-9]")
_NOISE = {"duralast", "assembly", "with", "kit", "for", "and", "the", "extended", "life", "ultra", "gold", "valucraft",
          "dorman", "permatex", "victor", "reinz", "fram", "bosch", "motorcraft", "acdelco", "standard", "santech",
          "cloyes", "failsafe", "maker", "pro", "premium", "maxx", "max", "series", "engine", "motor", "stp"}


def _plain(text) -> str:
    return _PLAIN.sub("", str(text or "").upper())


def _words(text) -> set[str]:
    return {w.lower().rstrip("s") for w in _WORD.findall(str(text or "")) if w.lower() not in _NOISE}


def compare(items: list[dict], lines) -> list[dict]:
    """One entry per part bought: the job part line that looks like it bills it (by part number first, then by
    shared words in the description), and a status: billed, under_cost, not_approved or not_on_job."""
    candidates = [line for line in lines if (line.item_type or "") == "part"]
    taken: dict[int, int] = {}
    out = []
    for item in items:
        best, best_score = None, 0
        numbers = [_plain(p) for p in item.get("parts") or [] if len(_plain(p)) >= 4]
        for line in candidates:
            desc = line.description or ""
            score = 0
            if any(n and n in _plain(desc) for n in numbers):
                score = 100
            else:
                score = len(_words(item.get("name")) & _words(desc))
            score -= taken.get(line.id, 0) * 50  # prefer a line not already matched to another part
            if score > best_score:
                best, best_score = line, score
        entry = {"item": item, "line": best, "status": "not_on_job", "billed_unit": None, "billed_total": None,
                 "margin": None}
        if best is not None:
            taken[best.id] = taken.get(best.id, 0) + 1
            unit = float(best.unit_price or 0)
            entry["billed_unit"] = round(unit, 2)
            entry["billed_total"] = round(unit * float(best.quantity or 0), 2)
            cost = item.get("cost")
            if not best.billable:
                entry["status"] = "not_approved"
            elif cost is not None and unit + 0.005 < cost:
                entry["status"] = "under_cost"
            else:
                entry["status"] = "billed"
            if cost is not None:
                entry["margin"] = round(unit - cost, 2)
        out.append(entry)
    return out


STATUS = {"billed": "Billed", "under_cost": "Billed under cost", "not_approved": "On the job, not approved yet",
          "not_on_job": "Not on this job"}
