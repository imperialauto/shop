"""Sorts a new email into an inbox tab and decides if it's worth an alert.

Rules only, on the sender and subject. Most mail is silent; an alert goes out for a lead or customer,
a U-Haul job offer, or a bill that says past due, cancelled or shutoff. The owner can move a thread to
another tab, and sorting leaves it there after that.
"""
import re
from dataclasses import dataclass


@dataclass
class Sort:
    category: str
    urgent: bool
    reason: str


def _any(text: str, words) -> str:
    for w in words:
        if re.search(w, text, re.I):
            return w
    return ""


UHAUL_SENDERS = (r"@([\w.-]+\.)?uhaul\.com$", r"@([\w.-]+\.)?uhaul\.net$")
UHAUL_URGENT = (r"roadside", r"dispatch", r"service request", r"\boffer\b", r"repair order", r"\bpo\b", r"tow")

LEAD_SENDERS = (r"@squarespace\.info$", r"form-submission@", r"@([\w.-]+\.)?thumbtack\.com$", r"@([\w.-]+\.)?yelp\.com$",
                r"@([\w.-]+\.)?angi\.com$", r"@([\w.-]+\.)?honkforhelp\.com$", r"@([\w.-]+\.)?honk\.(com|technology)$")
LEAD_SUBJECTS = (r"form submission", r"new (lead|request|message|inquiry)", r"quote request", r"estimate request",
                 r"appointment request", r"request a quote", r"contact form", r"service request")
LEAD_SKIP = (r"review", r"\bstats\b", r"performance", r"\btips?\b", r"newsletter", r"invoice", r"receipt")

BILL_SENDERS = (r"@([\w.-]+\.)?aps\.com$", r"@([\w.-]+\.)?srpnet\.com$", r"@([\w.-]+\.)?spectrum\.(com|net)$",
                r"@([\w.-]+\.)?att\.(com|net)$", r"@([\w.-]+\.)?verizon(wireless)?\.com$", r"@([\w.-]+\.)?citi\.com$",
                r"@([\w.-]+\.)?citibank\.com$", r"@([\w.-]+\.)?ncsecu\.org$", r"@([\w.-]+\.)?biberk\.com$",
                r"@([\w.-]+\.)?directauto\.com$", r"@([\w.-]+\.)?getflex\.com$", r"@([\w.-]+\.)?klarna\.com$",
                r"@([\w.-]+\.)?squarespace\.com$", r"@([\w.-]+\.)?railway\.(app|com)$", r"@([\w.-]+\.)?quo\.com$",
                r"@([\w.-]+\.)?openphone\.(com|co)$", r"@([\w.-]+\.)?apple\.com$", r"@([\w.-]+\.)?paypal\.com$")
BILL_SUBJECTS = (r"\bbill\b", r"statement", r"payment (due|reminder|received|scheduled|confirmation)", r"autopay",
                 r"past due", r"amount due", r"\binvoice\b", r"renew", r"subscription", r"premium")
BILL_URGENT = (r"past due", r"overdue", r"final notice", r"shut ?off", r"disconnect", r"cancel+ed", r"cancellation",
               r"payment (failed|declined|unsuccessful)", r"suspend", r"collection", r"lapse", r"expir(ed|es|ing)",
               r"couldn.t (process|charge)", r"action required")

VENDOR_SENDERS = (r"@([\w.-]+\.)?autozone(pro)?\.com$", r"@([\w.-]+\.)?partstech\.com$", r"@([\w.-]+\.)?fleetpride\.com$",
                  r"@([\w.-]+\.)?oreillyauto\.com$", r"@([\w.-]+\.)?napaonline\.com$", r"@([\w.-]+\.)?rockauto\.com$",
                  r"@([\w.-]+\.)?advanceautoparts\.com$", r"@([\w.-]+\.)?alldata\.com$", r"@([\w.-]+\.)?worldpac\.com$",
                  r"@([\w.-]+\.)?snapfinance\.com$", r"@([\w.-]+\.)?intuit\.com$", r"@([\w.-]+\.)?nuvo\w*\.com$",
                  r"@([\w.-]+\.)?carparts\.com$", r"@([\w.-]+\.)?1aauto\.com$")


def sort_message(from_address: str, subject: str, labels=(), known_customer: bool = False,
                 outgoing: bool = False) -> Sort:
    sender = (from_address or "").lower()
    subject = subject or ""
    labels = set(labels or ())
    if outgoing:
        return Sort("other", False, "sent by the shop")
    if _any(sender, UHAUL_SENDERS) or re.search(r"u-?haul", subject, re.I):
        hit = _any(subject, UHAUL_URGENT)
        return Sort("uhaul", bool(hit), "U-Haul job" if hit else "U-Haul")
    if known_customer:
        return Sort("leads", True, "from a customer")
    lead_sender = _any(sender, LEAD_SENDERS)
    lead_subject = _any(subject, LEAD_SUBJECTS)
    if (lead_sender or lead_subject) and not _any(subject, LEAD_SKIP):
        return Sort("leads", True, "new lead")
    bill_sender = _any(sender, BILL_SENDERS)
    if bill_sender or _any(subject, BILL_SUBJECTS):
        hit = _any(subject, BILL_URGENT)
        if bill_sender or hit:
            return Sort("bills", bool(hit), "bill needs attention" if hit else "bill")
    if _any(sender, VENDOR_SENDERS):
        return Sort("vendors", False, "parts or vendor")
    if "CATEGORY_PROMOTIONS" in labels or "CATEGORY_SOCIAL" in labels:
        return Sort("other", False, "promotion")
    return Sort("other", False, "other")
