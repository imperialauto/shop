"""Snap Finance on the public website: the Financing page, the apply button and the short notes
on the home, service, pricing, booking and estimate pages.

Wording follows Snap's own consumer page for Snap Loan (snapfinance.com/how-it-works) and the
requirements shown in the shop's Snap merchant portal. Snap's merchant rules forbid custom marketing
without Snap's approval, so keep claims to these facts, keep the footnotes beside them, and never put
Snap's text-to-apply short code on the website.

Setting website_financing ("false" turns every Snap mention off). The apply button uses the same
shop apply link the texts use (setting snap_apply_link, see app.snap_link), through /site/financing/apply
so the link can change in one place and clicks show in the logs.
"""
from app import business_settings, snap_link

SETTING = "website_financing"
APPLY_PATH = "/site/financing/apply"

FACTS = {
    "range": "$300 to $5,000",
    "requirements": (
        "18 or older",
        "An active checking account",
        "Income of at least $1,000 a month",
        "Live in a state where Snap Loan is offered (Arizona is one)",
        "A Social Security number or ITIN",
        "A driver's license or state ID",
        "A smartphone number and an email address",
    ),
}

FOOTNOTES = (
    "¹ Not all applicants are approved. While no credit history is required, Snap obtains information from "
    "consumer reporting agencies, and your score with those agencies may be affected.",
    "² Average approval amounts vary across product types and range from $300 to $5,000. Subject to underwriting "
    "qualification criteria.",
    "Financing is provided by Snap Finance and its bank partner, not by Imperial Auto Care. Snap-branded product "
    "offering includes retail installment contracts, bank installment loans, and lease-to-own financing.",
)


def enabled() -> bool:
    return (business_settings.raw(SETTING) or "").strip().lower() != "false"


def apply_link() -> str:
    return snap_link.link()


def context() -> dict | None:
    """What the templates need, or None when financing is switched off."""
    if not enabled():
        return None
    return {"apply_path": APPLY_PATH, "range": FACTS["range"], "requirements": FACTS["requirements"],
            "footnotes": FOOTNOTES}
