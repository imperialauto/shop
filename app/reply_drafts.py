"""Reply drafts for customer texts. Nothing here sends anything.

draft(context) writes the next reply from the facts in app/reply_context.build():
the newest customer text decides what is being asked, and the job's own state
(status, quote total, approvals, parts, appointment, balance) supplies the answer.
When the stored facts don't answer the question, the draft says so honestly
("let me check and get back to you shortly") and needs_info names what is missing,
so the owner sees it on the card. It never invents a price, a time or a repair fact.

clean_up(rough, context) turns what the owner typed or dictated into a tidy text.
With GROQ_API_KEY set (the AI path the shop already uses) it asks the model to
polish it, and keeps the result only if it adds no number, price or link that
wasn't already in his words or the job; otherwise, and without a key, it falls
back to rule-based cleanup (capitals, punctuation, shorthand, greeting).

Every draft ends with the sign-off "- Jae", once. Snap Finance is never added
here: the owner ticks "Offer Snap Finance" on the text when he wants it.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from app import reply_context
from app.dispatch import ARRIVAL, INFO, SAFETY, SHOP_DETAILS

SIGN_OFF = "- Jae"
MAX_LEN = 1500
_SIGN_OFF_TAIL = re.compile(r"(?:\s*[-–—~]+\s*(?:jae|jaelan|jay|imperial auto care)\.?\s*|\s+(?:thanks,?\s*)?jae\.?\s*)$", re.I)

ACK = re.compile(r"^\W*(ok(ay)?|k|kk|thanks?( you)?( so much)?|thank you|thx|ty|sounds good|perfect|great|got it|cool|"
                 r"will do|appreciate (it|you)|awesome|bet|alright|sure)\W*$", re.I)
APPROVE = re.compile(r"\b(go ahead|approved?|approving|do it|let'?s do it|proceed|go for it|yes,? (please|go|do it)|"
                     r"green light)\b", re.I)
DECLINE = re.compile(r"\b(no thanks|hold off|not right now|too (much|expensive|high)|decline\w*|i'?ll pass|"
                     r"don'?t do (it|that))\b", re.I)
PICKUP = re.compile(r"\b(pick (it |him |her |them )?up|pickup|pick-up|come get|grab (it|my))\b", re.I)
WHEN = re.compile(r"\b(when|how long|eta|what time|today|tonight|tomorrow|by (monday|tuesday|wednesday|thursday|"
                  r"friday|saturday|the weekend|end of)|how soon)\b", re.I)
STATUS = re.compile(r"\b(status|ready|done|finished|update|progress|how'?s it (going|looking)|any news|"
                    r"still (working|waiting))\b", re.I)
PRICE = re.compile(r"\b(how much|price|pricing|cost|total|quote|estimate|owe|balance|pay|payment)\b", re.I)
PARTS = re.compile(r"\b(parts?|ordered|shipping|shipped|arriv\w*)\b", re.I)
SCHEDULE = re.compile(r"\b(appointment|schedule\w*|book\w*|available|availability|come in|bring it|drop (it )?off|"
                      r"what day)\b", re.I)
PHOTO = re.compile(r"^\[(customer )?sent a photo\]$", re.I)
REPAIR = re.compile(r"\b(noise|leak\w*|check engine|light|shak\w*|won'?t|broke\w*|problem|issue|start\w*|"
                    r"brakes?|a/?c|overheat\w*|diesel|transmission|engine)\b", re.I)

STATUS_WORDS = {
    "intake": "is checked in and in line for diagnosis",
    "diagnosing": "is being diagnosed now",
    "waiting_parts": "is waiting on parts",
    "in_progress": "is being worked on now",
    "waiting_approval": "has an estimate ready for you",
    "approval_received": "is approved and lined up",
    "complete": "is done and ready for pickup",
}
HOURS = "We're open Mon-Fri 11 AM-7 PM and Sat 12-5 PM."


@dataclass
class Draft:
    text: str
    intent: str
    needs_info: list[str] = field(default_factory=list)
    source: str = "rules"


def money(value) -> str:
    return f"${float(value):,.2f}"


def sign(text: str) -> str:
    """The text with one "- Jae" at the end, whatever sign-off it had before."""
    body = (text or "").strip()
    while True:
        trimmed = _SIGN_OFF_TAIL.sub("", body).rstrip()
        if trimmed == body:
            break
        body = trimmed
    return f"{body}\n{SIGN_OFF}" if body else ""


def _greeting(context: dict) -> str:
    style = context.get("style") or {}
    name = (context.get("customer") or {}).get("first_name")
    word = style.get("greeting", "Hi")
    if word is None:
        return ""
    if name and style.get("uses_name", True):
        return f"{word} {name}, "
    return f"{word}, "


def _cap(sentence: str) -> str:
    return sentence[:1].upper() + sentence[1:] if sentence else sentence


def _join(context: dict, sentences: list[str]) -> str:
    sentences = [s.strip() for s in sentences if s and s.strip()]
    if not sentences:
        return ""
    greet = _greeting(context)
    first = sentences[0]
    if greet and not _KEEP_CASE.match(first):
        first = first[:1].lower() + first[1:]
    text = greet + first if greet else _cap(first)
    text = " ".join([text] + sentences[1:])
    if not (context.get("style") or {}).get("exclaims"):
        text = re.sub(r"!+", ".", text)
    return sign(text)


def _car(context: dict, job: dict | None) -> str:
    label = (job or {}).get("vehicle") or ((context.get("vehicle") or {}).get("label"))
    return f"your {label}" if label else "your vehicle"


def _day(iso_value: str | None, with_time: bool = False) -> str | None:
    when = reply_context.shop_time(iso_value)
    if when is None:
        return None
    day = f"{when:%A, %b} {when.day}"
    if with_time:
        day += f" at {when:%I:%M %p}".replace(" 0", " ")
    return day


def _pending_link(job: dict) -> list[str]:
    total = job.get("total")
    link = job.get("status_link")
    out = [f"Your estimate comes to {money(total)}." if total else "Your estimate is ready."]
    if link:
        out.append(f"You can review and approve it here: {link}")
    return out


def _parts_state(job: dict) -> tuple[list[str], list[str]]:
    parts = job.get("parts") or []
    waiting = [p for p in parts if p.get("status") != "received"]
    if parts and not waiting:
        return ["All the parts are in, so we're getting back on it."], []
    if waiting:
        names = " and ".join(_the(p["description"]) for p in waiting[:2])
        return [f"We're still waiting on {names}.", "I'll let you know as soon as I have an arrival date."], \
            [f"parts ETA ({names}): none recorded"]
    return ["We're waiting on parts.", "I'll let you know as soon as I have an arrival date."], \
        [f"parts ETA: no parts order on job #{job['id']}"]


def _the(description: str) -> str:
    return description if re.match(r"^(a|an|the|your|\d)\b", description, re.I) else f"the {description}"


def _ready(job: dict, context: dict) -> list[str]:
    out = [f"{_cap(_car(context, job))} is done and ready for pickup."]
    invoice = job.get("invoice")
    if invoice and invoice.get("balance"):
        out.append(f"Your balance is {money(invoice['balance'])}.")
    elif job.get("total") and not invoice:
        left = round(job["total"] - (job.get("deposits_paid") or 0), 2)
        if left > 0:
            out.append(f"The balance is {money(left)}.")
    out.append(HOURS)
    return out


def _status(job: dict, context: dict, asked_when: bool) -> tuple[list[str], list[str]]:
    status = job.get("status") or "intake"
    pending = (job.get("approvals") or {}).get("pending", 0)
    if status == "complete":
        return _ready(job, context), []
    if status == "waiting_approval" or pending:
        needs = [] if job.get("status_link") else [f"status link: job #{job['id']} has none"]
        return _pending_link(job), needs
    if status == "waiting_parts":
        return _parts_state(job)
    out = [f"{_cap(_car(context, job))} {STATUS_WORDS.get(status, 'is in the shop')}."]
    needs = []
    if asked_when:
        promised = _day(job.get("promised_at"))
        if promised:
            out.append(f"We're aiming to have it done {promised}.")
        else:
            out.append("Let me check on timing and get back to you shortly.")
            needs.append(f"finish time: no promised date on job #{job['id']}")
    else:
        out.append("I'll keep you posted.")
    return out, needs


def draft(context: dict) -> Draft:
    """The rule-based reply to the newest customer text."""
    text = reply_context.latest_inbound(context)
    job = context.get("job")
    vehicle = context.get("vehicle")
    first = (context.get("customer") or {}).get("first_name")
    snippet = " ".join(text.split())[:60]

    def done(intent, sentences, needs=()):
        return Draft(_join(context, sentences)[:MAX_LEN], intent, list(needs))

    if not text:
        return done("none", ["Thanks for reaching out to Imperial Auto Care. How can I help?"],
                    ["no customer text found to answer"])
    if SAFETY.search(text):
        return done("safety", ["Please don't drive it while it's doing that.",
                               "I'll give you a call shortly to figure out the next step."],
                    ["safety concern: call the customer"])
    if ARRIVAL.search(text):
        return done("arrival", ["Thanks for letting me know, I'll be right with you."],
                    ["customer is here or waiting: respond now"])
    if PHOTO.match(text):
        return done("photo", ["Got the photo, thanks.", "I'll take a look and get back to you shortly."],
                    ["look at the photo they sent"])
    if ACK.match(text):
        return done("thanks", [f"Sounds good{', ' + first if first and not _greeting(context) else ''}, thanks."])
    if INFO.search(text) and not (PRICE.search(text) or STATUS.search(text)):
        return done("hours", [SHOP_DETAILS])

    if job:
        jid = job["id"]
        pending = (job.get("approvals") or {}).get("pending", 0)
        if APPROVE.search(text):
            needs = [f"record the approval on job #{jid}: {pending} line(s) still pending"] if pending else []
            return done("approve", ["Thanks, got it.", "I'll get started and keep you posted."], needs)
        if DECLINE.search(text):
            return done("decline", ["No problem, thanks for letting me know."],
                        [f"customer declined: update job #{jid}"])
        if PICKUP.search(text):
            if job.get("status") == "complete":
                return done("pickup", _ready(job, context))
            out, needs = _status(job, context, True)
            return done("pickup", out + ["I'll text you as soon as it's ready."], needs)
        if PRICE.search(text):
            invoice = job.get("invoice")
            if invoice:
                return done("price", [f"Your balance is {money(invoice['balance'])}."
                                      if invoice.get("balance") else "You're all paid up, thanks."])
            if job.get("total"):
                return done("price", _pending_link(job) if pending else
                            [f"The total on {_car(context, job)} comes to {money(job['total'])}."])
            return done("price", ["I don't have a price on it yet.",
                                  "I'll get you a quote as soon as the diagnosis is done."],
                        [f"quote: no priced lines on job #{jid}"])
        if PARTS.search(text) and job.get("status") == "waiting_parts":
            out, needs = _parts_state(job)
            return done("parts", out, needs)
        if SCHEDULE.search(text):
            appt = job.get("appointment")
            if appt:
                return done("schedule", [f"You're set for {_day(appt['starts_at'], True)}."])
            return done("schedule", ["What day and time works best for you?"],
                        [f"pick a time: no appointment on job #{jid}"])
        if STATUS.search(text) or WHEN.search(text):
            out, needs = _status(job, context, bool(WHEN.search(text)))
            return done("status", out, needs)
        return done("general", ["Thanks, let me check on that and get back to you shortly."],
                    [f"nothing on file answers: “{snippet}”"])

    # No open job for this customer.
    if STATUS.search(text) or PICKUP.search(text) or APPROVE.search(text):
        return done("status", ["Let me check on that and get back to you shortly."],
                    ["no open job on file for this customer"])
    if PRICE.search(text):
        out = ["I'd need to take a look at it before I can give you a price."]
        out.append("What day works for you to bring it in?" if vehicle else
                   "What's the year, make and model, and what's it doing?")
        return done("price", out, ["price asked: nothing quoted yet"])
    if SCHEDULE.search(text):
        return done("schedule", ["What day and time works best for you?" if vehicle else
                                 "What's the year, make and model, and what day works best for you?"],
                    ["pick a time: no appointment on file"])
    if REPAIR.search(text):
        if vehicle:
            return done("repair", ["Thanks for the details.", "Can you bring it in so I can take a look? What day works for you?"])
        return done("repair", ["Thanks for reaching out to Imperial Auto Care.",
                               "What's the year, make and model?"])
    return done("general", ["Thanks, let me check on that and get back to you shortly."],
                [f"nothing on file answers: “{snippet}”"])


# ── Clean-up of a rough reply ──────────────────────────────────────────────

_SHORTHAND = [
    (r"\bu\b", "you"), (r"\bur\b", "your"), (r"\bthx\b", "thanks"), (r"\bty\b", "thank you"),
    (r"\bpl[sz]\b", "please"), (r"\b(tmrw|tmrrw|tmr|2morrow)\b", "tomorrow"), (r"\bim\b", "I'm"),
    (r"\bive\b", "I've"), (r"\bdont\b", "don't"), (r"\bcant\b", "can't"), (r"\bwont\b", "won't"),
    (r"\bdidnt\b", "didn't"), (r"\bdoesnt\b", "doesn't"), (r"\bisnt\b", "isn't"), (r"\bthats\b", "that's"),
    (r"\byoure\b", "you're"), (r"\bb4\b", "before"), (r"\bw/\s?", "with "), (r"\bappt\b", "appointment"),
    (r"\bits (?=(ready|done|here|fixed|good|going|all|at|in|on|not|been|still|almost|running)\b)", "it's "),
    (r"\bi\b", "I"),
]
_SPOKEN = [(r"\s*\b(question mark)\b", "?"), (r"\s*\b(exclamation (point|mark))\b", "!"),
           (r"\s*\b(period|full stop)\b", "."), (r"\s*\bcomma\b", ","), (r"\s*\bnew (line|paragraph)\b", ".")]
_RELAY = re.compile(r"^\s*(?:(?:can you |please |just )?(?:tell|let|text|say to|ask)\s+(?:him|her|them|the customer|"
                    r"[A-Z][a-z]+)\s+(?:know\s+)?(?:that\s+)?|say\s+(?:that\s+)?)", re.I)
_PRONOUNS = [(r"\bthey're\b", "you're"), (r"\btheir\b", "your"), (r"\bthem\b", "you"), (r"\bthey\b", "you"),
             (r"\bhe's\b", "you're"), (r"\bshe's\b", "you're"), (r"\bhis\b", "your"), (r"\bhim\b", "you"),
             (r"\bhe\b", "you"), (r"\bshe\b", "you"), (r"\bher\b", "your")]
_QUESTION = re.compile(r"^(what|when|where|which|who|why|how|can|could|do|does|did|is|are|will|would|should|want|"
                       r"you want|any chance)\b", re.I)
_KEEP_CASE = re.compile(r"^(I\b|I'|[A-Z]{2,}|(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|"
                        r"Sep|Oct|Nov|Dec|Imperial|Snap|Ford|Ram|Chevy|Dodge|GMC|Toyota|Honda|Nissan|Mercedes|BMW|Jeep)")
_GREETED = re.compile(r"^(hi|hey|hello|good (morning|afternoon|evening)|morning|yo)\b", re.I)


def rule_clean_up(rough: str, context: dict | None = None) -> str:
    """Tidy his words without changing what they say."""
    context = context or {}
    text = re.sub(r"\s+", " ", rough or "").strip()
    text = _SIGN_OFF_TAIL.sub("", text).strip()
    if not text:
        return ""
    relayed = _RELAY.match(text)
    if relayed:
        text = text[relayed.end():]
        for pattern, repl in _PRONOUNS:
            text = re.sub(pattern, repl, text, flags=re.I)
    if not re.search(r"[.!?,]", text):  # dictated: spoken punctuation only means something here
        for pattern, repl in _SPOKEN:
            text = re.sub(pattern, repl, text, flags=re.I)
    for pattern, repl in _SHORTHAND:
        text = re.sub(pattern, repl, text, flags=re.I)
    name = (context.get("customer") or {}).get("first_name")
    if name:
        text = re.sub(rf"\b{re.escape(name)}\b", name, text, flags=re.I)
        text = re.sub(rf"^((?:hi|hey|hello)\s+{re.escape(name)})\s+(?=[a-z])", r"\1, ", text, flags=re.I)
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    text = re.sub(r"([,.!?])(?=[A-Za-z])", r"\1 ", text)
    text = re.sub(r"\.{2,}", ".", text)
    if not (context.get("style") or {}).get("exclaims"):
        text = re.sub(r"!{2,}", "!", text)
    if text and text[-1] not in ".!?":
        last = re.split(r"(?<=[.!?])\s+", text)[-1]
        text += "?" if _QUESTION.match(last) else "."
    sentences = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(_cap(s) for s in sentences)
    if not _GREETED.match(text) and context:
        greet = _greeting(context)
        if greet:
            body = text if _KEEP_CASE.match(text) else text[:1].lower() + text[1:]
            text = greet + body
    return sign(text)[:MAX_LEN]


def ai_configured() -> bool:
    return bool(os.getenv("GROQ_API_KEY", "").strip()) and os.getenv("REPLY_AI", "on").strip().lower() != "off"


CLEAN_UP_PROMPT = """You rewrite a rough text message that Jae, owner of Imperial Auto Care (an auto repair shop), \
typed or dictated to a customer. Make it a clean, friendly, short SMS in his voice: warm, direct, plain words, \
1-3 sentences, no emoji, at most one exclamation mark. Keep his meaning exactly. Do not add any price, number, \
date, time, link, promise or repair detail that is not in his words. If he says to tell the customer something, \
write it to the customer directly. Greet the customer by first name if one is given. Do not sign off; the app adds \
"- Jae". Output only the message."""


def _facts_line(context: dict) -> str:
    job = context.get("job") or {}
    parts = []
    name = (context.get("customer") or {}).get("first_name")
    if name:
        parts.append(f"Customer first name: {name}")
    if job:
        parts.append(f"Job status: {job.get('status')}")
    return "\n".join(parts)


def _ai_complete(system: str, user: str) -> str:
    """One chat call on the shop's existing Groq setup. Patched in tests: no network there."""
    from groq import Groq
    from app.groq_models import text_model
    client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=12.0)
    response = client.chat.completions.create(
        model=text_model(), max_tokens=300,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return (response.choices[0].message.content or "").strip()


_NUMBERISH = re.compile(r"\$?\d[\d,.:]*|https?://\S+")


def _adds_facts(candidate: str, rough: str, context: dict) -> bool:
    """True when the model put in a number, price, time or link he never gave."""
    allowed = set(_NUMBERISH.findall(rough or ""))
    job = context.get("job") or {}
    for value in (job.get("total"), (job.get("invoice") or {}).get("balance")):
        if value:
            allowed.update({money(value), f"{float(value):,.2f}", str(value)})
    if job.get("status_link"):
        allowed.add(job["status_link"])
    normalize = lambda s: s.rstrip(".,:").lstrip("$")
    allowed_norm = {normalize(a) for a in allowed}
    return any(normalize(found) not in allowed_norm for found in _NUMBERISH.findall(candidate))


def clean_up(rough: str, context: dict | None = None) -> Draft:
    """His rough words as a polished text. AI when configured, rules otherwise or if the AI adds facts."""
    context = context or {}
    fallback = rule_clean_up(rough, context)
    if not fallback:
        return Draft("", "clean_up", ["type or dictate something first"])
    if ai_configured():
        try:
            user = f"{_facts_line(context)}\n\nJae's rough message:\n{rough.strip()[:1000]}"
            candidate = _ai_complete(CLEAN_UP_PROMPT, user).strip().strip('"').strip()
            if candidate and len(candidate) <= 600 and not _adds_facts(candidate, rough, context):
                return Draft(sign(candidate)[:MAX_LEN], "clean_up", [], "ai")
        except Exception:  # noqa: BLE001 - any AI trouble falls back to the rule cleanup
            pass
    return Draft(fallback, "clean_up", [], "rules")


def card_draft(item, context: dict) -> Draft:
    """What the reply box shows: the AI draft the messaging service saved for this exact
    message (signed "- Jae"), or the rule draft. needs_info always comes from the rules."""
    rules = draft(context)
    saved = (getattr(item, "draft_reply", None) or "").strip()
    if saved:
        return Draft(sign(saved)[:MAX_LEN], rules.intent, rules.needs_info, "ai")
    return rules
