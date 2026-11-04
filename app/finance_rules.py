"""Built-in classification. Only public merchants and generic bank wording belong
here: this repository is public. Customer and personal names go in fin_rules."""
import re
from dataclasses import dataclass

REVENUE, COGS, OPEX, CAPEX = "revenue", "cogs", "opex", "capex"
DEBT, INTEREST, CONTRIB, DRAW = "debt_principal", "interest", "owner_contribution", "owner_draw"
PERSONAL, TRANSFER, UNCONFIRMED, UNCLASSIFIED = "personal", "transfer", "unconfirmed", "unclassified"
GROUPS = [REVENUE, COGS, OPEX, CAPEX, DEBT, INTEREST, CONTRIB, DRAW, PERSONAL, TRANSFER, UNCONFIRMED, UNCLASSIFIED]
CAPEX_REVIEW_CENTS = 250000
CATEGORIES = [
    "Repair and service revenue", "Fleet and contract revenue", "Vehicle rental revenue",
    "Card processor payouts", "Repair parts, tires, fluids and supplies", "Subcontracted repairs and towing",
    "Shop rent", "Utilities and phone", "Insurance", "Mobile service fuel and vehicle operating",
    "Tools, equipment and capital purchases", "Shop supplies", "Diagnostic and shop software",
    "Advertising and marketing", "Payment processing and banking fees", "Licensing and professional services",
    "Payroll and contractor pay", "Vehicle rental operations", "Taxes", "Financing costs", "Debt principal",
    "Owner contribution", "Owner withdrawal", "Personal spending", "Account transfer",
    "Unconfirmed receipt", "Unclassified",
]


@dataclass(frozen=True)
class Result:
    group: str
    category: str
    designation: str = "business"
    division: str = "unassigned"
    confidence: str = "high"
    needs_review: bool = False
    reason: str = ""


_RULES = [(re.compile(p, re.I), r) for p, r in [
    (r"online (banking )?transfer|overdraft protection|transfer (from|to) (chk|sav)", Result(TRANSFER, "Account transfer", "business")),
    (r"zelle payment from imperial auto care|zelle payment from imperial", Result(TRANSFER, "Account transfer", "business")),
    (r"service charges for|overdraft item fee|fee reversal|withdrwl eft.*fee|non-chase atm fee", Result(OPEX, "Payment processing and banking fees")),
    (r"bankcard.*(mthly|monthly) fees", Result(OPEX, "Payment processing and banking fees")),
    (r"bankcard.*settlement|intuit|real time payment.*stripe|stripe|swipesimple|square inc", Result(REVENUE, "Card processor payouts", "business", "repair", "likely")),
    (r"u-haul", Result(REVENUE, "Fleet and contract revenue", "business", "repair", "likely")),
    (r"outdoorsy|wheelbase|turo", Result(REVENUE, "Vehicle rental revenue", "business", "rental", "likely")),
    (r"bouncie|roamly", Result(OPEX, "Vehicle rental operations", "business", "rental")),
    (r"autozone|o'?reilly|napa|advance auto|fleetpride|freightliner|rockauto|mercedes ?benz|tire shop|discount tire", Result(COGS, "Repair parts, tires, fluids and supplies", "business", "repair")),
    (r"harbor freight|home depot|lowe'?s|bj tools|snap.?on|matco|mac tools", Result(CAPEX, "Tools, equipment and capital purchases", "business", "repair")),
    (r"towing|tow service", Result(COGS, "Subcontracted repairs and towing", "business", "repair", "likely")),
    (r"circle k|qt \d|quiktrip|chevron|shell|7-eleven|arco|maverik|love's|pilot|sheetz|wawa|frys fuel|samsclub.*gas|phillips 66|buc-ee|citgo|\bfuel\b", Result(OPEX, "Mobile service fuel and vehicle operating", "business", "repair", "likely")),
    (r"srp power|\bsrp\b|aps\b|cox |at&t|att payment|verizon|t-mobile", Result(OPEX, "Utilities and phone", "business", "repair", "likely")),
    (r"natl liab|national liab|progressive|geico|state farm|liberty mutual|direct auto ins", Result(OPEX, "Insurance", "business", "unassigned", "likely")),
    (r"squarespace|sqsp|google \*worksp|claude\.ai|anthropic|autoauth|alldata|mitchell|shopmonkey|tekmetric|quickbooks|openphone|\bquo\b", Result(OPEX, "Diagnostic and shop software")),
    (r"nav tech|nav\.com|legalzoom|\bcpa\b|registrar|azcc", Result(OPEX, "Licensing and professional services", "business", "unassigned", "likely")),
    (r"car ?wash", Result(OPEX, "Mobile service fuel and vehicle operating", "business", "repair", "likely")),
    (r"amazon|amzn", Result(UNCLASSIFIED, "Unclassified", "unknown", "unassigned", "review", True, "Amazon order: parts for a job, or personal?")),
    (r"facebk|facebook|meta ?ads|google ads|\bmarketing\b|\byelp\b", Result(OPEX, "Advertising and marketing")),
    (r"azdor|arizona dept|az dept of rev|irs |usataxpymt", Result(OPEX, "Taxes", "business", "unassigned", "likely", True, "Confirm which tax this payment covers")),
    (r"kpt\*|katapult|flexible finance|flex\* |snap finance|affirm|klarna|afterpay", Result(OPEX, "Financing costs", "mixed", "unassigned", "medium", True, "Financing: what was bought, and is it business?")),
    (r"apple cash|apple cash inst|venmo|cash app|paypal \*", Result(UNCLASSIFIED, "Unclassified", "unknown", "unassigned", "review", True, "Peer payment: who received or sent this, and why?")),
    (r"atm withdrawal|atm withdraw|withdrwl eft", Result(DRAW, "Owner withdrawal", "business", "unassigned", "likely", True, "Cash taken out: business purchase or owner draw?")),
    (r"atm cash deposit|bkofamerica atm|cash deposit", Result(UNCONFIRMED, "Unconfirmed receipt", "business", "unassigned", "medium", True, "Cash deposit: customer payment or owner money?")),
    (r"dutch ?bros|doordash|dd \*|whataburger|chick-fil-a|raising canes|mcdonald|wendys|in-n-out|little caesars|5guys|sushi|tst\*|culvers|dunkin|waffle house|taquitos|dutchbros", Result(PERSONAL, "Personal spending", "personal", "unassigned", "likely")),
    (r"frys-food|safeway|winco|wal-?mart|wm supercenter|instacart|target|dollar ?tree|sams club|albertson", Result(PERSONAL, "Personal spending", "personal", "unassigned", "medium")),
    (r"airbnb|round1|vape|locs by|cvs|walgreens|nayax|apple\.com/bill|giveon|td spot|uber|lyft|ysi\*", Result(PERSONAL, "Personal spending", "personal", "unassigned", "medium")),
    (r"card purchase return|purchase refund|checkcard .*refund", Result(COGS, "Repair parts, tires, fluids and supplies", "business", "repair", "medium")),
]]


def owner_group_for_receipt(cents: int) -> str:
    return CONTRIB if cents > 0 else DRAW


def classify(description: str, cents: int, private_rules=()) -> Result:
    text = " ".join((description or "").split())
    text = re.sub(r"\bZe[l1i]{2}e\b", "Zelle", text, flags=re.I)  # common OCR misreads
    for rule in private_rules:
        if rule.pattern.lower() in text.lower():
            return Result(rule.report_group, rule.category, rule.designation, rule.division,
                          "review" if rule.needs_review else "owner_rule", bool(rule.needs_review), rule.note or "")
    if cents < 0 and re.search(r"outdoorsy|wheelbase", text, re.I):
        return Result(OPEX, "Vehicle rental operations", "business", "rental", "likely")
    if re.search(r"orig co name: ?bank[ceo]?[ao]?r?d", text, re.I):  # bank-card processor, often garbled by OCR
        if cents > 0:
            return Result(REVENUE, "Card processor payouts", "business", "repair", "likely")
        return Result(OPEX, "Payment processing and banking fees")
    if re.search(r"purchase return|purchase refund|\brefund\b", text, re.I) and cents > 0:
        if re.search(r"autozone|o'?reilly|napa|fleetpride|advance", text, re.I):
            return Result(COGS, "Repair parts, tires, fluids and supplies", "business", "repair", "high")
        return Result(PERSONAL, "Personal spending", "personal", "unassigned", "medium")
    for pattern, result in _RULES:
        if pattern.search(text):
            if result.group == CAPEX and abs(cents) >= CAPEX_REVIEW_CENTS:
                return Result(CAPEX, result.category, "business", result.division, "medium", True, "Large purchase: confirm it is a capital item")
            if result.group == CAPEX:  # small tools are expensed, not capitalised
                return Result(OPEX, "Shop supplies", "business", result.division, "high")
            return result
    if re.search(r"zelle payment from", text, re.I) and cents > 0:
        return Result(REVENUE, "Repair and service revenue", "business", "repair", "likely")
    if re.search(r"zelle payment to", text, re.I):
        return Result(UNCLASSIFIED, "Unclassified", "unknown", "unassigned", "review", True, "Who was paid, and for what?")
    if cents > 0:
        return Result(UNCONFIRMED, "Unconfirmed receipt", "unknown", "unassigned", "review", True, "Money in with no recognised source")
    return Result(UNCLASSIFIED, "Unclassified", "unknown", "unassigned", "review", True, "Merchant not recognised")
