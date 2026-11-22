"""The optional Snap Finance line for customer texts.

Not every customer needs financing, so the line goes in only when the owner ticks
"Offer Snap Finance" on the text. The apply link itself is never added here: it goes
out only when a customer asks for it.
"""
import re

LINE = "We also offer Snap Finance if you'd like to make payments. Just ask and I'll send the link."

# Sign-offs the shop's texts end with; the Snap line goes just before them.
_SIGN_OFF = re.compile(r"(\s*(?:-\s*)?(?:Jae|Imperial Auto Care)\s*)$")


def checked(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "on", "yes")


def mentioned(text: str | None) -> bool:
    return "snap" in (text or "").lower()


def add(text: str, offer: bool) -> str:
    """Adds the Snap line when asked, once, before the sign-off."""
    text = (text or "").rstrip()
    if not offer or not text or mentioned(text):
        return text
    match = _SIGN_OFF.search(text)
    if match:
        body = text[:match.start()].rstrip()
        return f"{body} {LINE}{match.group(1)}" if body else f"{LINE}{match.group(1)}"
    return f"{text} {LINE}"
