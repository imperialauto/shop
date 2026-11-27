"""Purchase order numbers for jobs, so a parts store receipt can be matched back to its job.

Every job's PO comes straight from its job number: job 21 is IAC-PO-0021. Nothing is stored, so
every job has one the moment it exists, from every place jobs are opened (the job page, website
bookings, the assistant), old jobs included, and two jobs can never share one.

Parts stores print whatever was typed in their PO box, and counter staff often shorten it, so
reading one back accepts "IAC-PO-0021", "IACPO21", "PO 0021", "P.O. #21" and the like.
"""
import re

PREFIX = "IAC-PO-"
WIDTH = 4

# "IAC-PO-0021", "IAC PO 21", "IACPO0021", "PO# 0021", "P.O. 21", "PO: 21", "Purchase order 21".
_PO = re.compile(r"(?<![A-Z0-9])(?:IAC[\s\-_]*)?(?:P\.?\s?O\.?|PURCHASE\s+ORDER)\s*(?:NO\.?|NUMBER|NUM)?\s*[#:\-]?\s*(\d{1,7})(?!\d)",
                 re.IGNORECASE)
_STRICT = re.compile(r"IAC[\s\-_]*PO[\s\-_]*(\d{1,7})(?!\d)", re.IGNORECASE)


def for_job(job_id: int | None) -> str:
    """The PO to give a parts store for this job."""
    if not job_id:
        return ""
    return f"{PREFIX}{int(job_id):0{WIDTH}d}"


def parse(text: str | None) -> int | None:
    """The job number in a PO someone typed or a receipt printed, or None if it isn't a PO.

    A bare number ("21") is not treated as a PO here, because names and part numbers are
    searched in the same boxes."""
    match = _PO.search((text or "").strip())
    if not match:
        return None
    number = int(match.group(1))
    return number or None


def find_in_text(text: str | None) -> list[int]:
    """Job numbers of our own POs (IAC-PO-...) printed anywhere in a receipt's text, first seen first.

    Only the shop's own prefix counts here: a receipt also carries the store's invoice and order
    numbers, and a loose "PO 1234" there could point at the wrong job."""
    seen = []
    for match in _STRICT.finditer(text or ""):
        number = int(match.group(1))
        if number and number not in seen:
            seen.append(number)
    return seen
