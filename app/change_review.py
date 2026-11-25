"""Owner's Changes page: code changes (GitHub pull requests) waiting on the owner, and his yes or not-now on each.

Reads pull requests from GitHub, read only, cached for a few minutes. The repository is
CHANGES_GITHUB_REPO (default imperialauto/shop). CHANGES_GITHUB_TOKEN is optional: a read-only
token only raises GitHub's limit on lookups; the page works without one.

The owner's decision is saved in business_settings under "change_approvals", which keeps every
old and new value with who changed it, so there is no database change. A yes is tied to the
exact version (commit) he saw: if the change is updated afterwards, the page asks him to look again.

Approving here only records the owner's yes. Nothing is merged, deployed or turned on from this page.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import business_settings

SETTING_KEY = "change_approvals"
DEFAULT_REPO = "imperialauto/shop"
CACHE_SECONDS = 300
TIMEOUT = 8.0
MAX_CHECKED = 30  # check results are looked up for at most this many open changes
RECENT_DAYS = 7
DECISIONS = {"approved": "Approved", "not_now": "Not now"}
NOTE_MAX = 300

SECTIONS = (
    ("changed", "Changed since you approved", "It was updated after your yes. Look again before it goes live."),
    ("ready", "Waiting on your yes", "Checks passed. Open it, then approve or set it aside."),
    ("failing", "Needs a fix first", "A check failed. Claude fixes these before asking you."),
    ("running", "Still being worked on", "Checks are running or the change is being built."),
    ("approved", "You approved", "Waiting to be merged and made live. That still needs a final go from you."),
    ("not_now", "Set aside", "You said not now. Nothing happens with these."),
)

_cache: dict = {"at": 0.0, "data": None}
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def repo() -> str:
    value = os.getenv("CHANGES_GITHUB_REPO", "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value or "") else DEFAULT_REPO


def clear_cache():
    _cache.update(at=0.0, data=None)


def _get(path: str, params: dict | None = None):
    """One read-only GitHub API call. Returns the parsed JSON. Raises httpx.HTTPError on failure."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "imperial-operations-dashboard"}
    token = os.getenv("CHANGES_GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get(f"https://api.github.com{path}", params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary(body: str | None) -> str:
    """The plain-language part of a description: its "After:" paragraph if there is one, else its first paragraph."""
    text = (body or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pick = next((p for p in paragraphs if re.match(r"^\**after:?\**", p, re.IGNORECASE)), None)
    if pick is None:
        pick = next((p for p in paragraphs if not p.startswith(("#", "<!--", "|", "```"))), "")
    pick = re.sub(r"^\**after:?\**\s*", "", pick, flags=re.IGNORECASE)
    pick = re.sub(r"[*_`#>]", "", " ".join(pick.split()))
    return pick[:280] + ("…" if len(pick) > 280 else "")


def checks_state(runs: list[dict]) -> str:
    """'passed', 'failing', 'running' or 'none' from a commit's check runs."""
    if not runs:
        return "none"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    bad = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
    if any(r.get("conclusion") in bad for r in runs):
        return "failing"
    return "passed"


def _change(pr: dict, checks: str) -> dict:
    head = pr.get("head") or {}
    return {
        "number": int(pr["number"]), "title": str(pr.get("title") or "")[:200],
        "url": str(pr.get("html_url") or ""), "draft": bool(pr.get("draft")),
        "branch": str(head.get("ref") or ""), "sha": str(head.get("sha") or ""),
        "updated_at": _when(pr.get("updated_at")), "summary": _summary(pr.get("body")),
        "checks": checks,
    }


def fetch(force: bool = False, now: float | None = None) -> dict:
    """Open changes and those merged in the last week. On a GitHub failure the last good list is kept."""
    now = time.monotonic() if now is None else now
    if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    name = repo()
    try:
        open_prs = _get(f"/repos/{name}/pulls", {"state": "open", "per_page": 50, "sort": "updated", "direction": "desc"})
        changes = []
        for i, pr in enumerate(open_prs):
            state = "none"
            sha = (pr.get("head") or {}).get("sha") or ""
            if i < MAX_CHECKED and _SHA.match(sha):
                runs = _get(f"/repos/{name}/commits/{sha}/check-runs", {"per_page": 100}).get("check_runs") or []
                state = checks_state(runs)
            changes.append(_change(pr, state))
        closed = _get(f"/repos/{name}/pulls", {"state": "closed", "per_page": 30, "sort": "updated", "direction": "desc"})
        since = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
        merged = [{"number": int(pr["number"]), "title": str(pr.get("title") or "")[:200],
                   "url": str(pr.get("html_url") or ""), "merged_at": _when(pr.get("merged_at"))}
                  for pr in closed if _when(pr.get("merged_at")) and _when(pr.get("merged_at")) >= since][:15]
        data = {"changes": changes, "merged": merged, "error": None, "fetched_at": datetime.now(timezone.utc), "repo": name}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        limited = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 429)
        error = ("GitHub's lookup limit was reached. The list will come back within the hour."
                 if limited else "Couldn't reach GitHub just now. Try Refresh in a minute.")
        old = _cache["data"]
        data = dict(old, error=error) if old else {"changes": [], "merged": [], "error": error,
                                                  "fetched_at": None, "repo": name}
        _cache.update(at=now, data=data)
        return data
    _cache.update(at=now, data=data)
    return data


def decisions() -> dict:
    """{"249": {"decision": "approved", "sha": "...", "at": "...", "by": "...", "note": "..."}}"""
    try:
        value = json.loads(business_settings.raw(SETTING_KEY) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def record(db, number: int, sha: str, decision: str, note: str, actor: str) -> dict:
    """Saves the owner's decision on one change (or clears it). Caller commits."""
    if number <= 0:
        raise ValueError("Unknown change.")
    if decision not in DECISIONS and decision != "clear":
        raise ValueError("Unknown decision.")
    if decision != "clear" and not _SHA.match(sha or ""):
        raise ValueError("Refresh the page and try again.")
    current = decisions()
    if decision == "clear":
        current.pop(str(number), None)
    else:
        current[str(number)] = {"decision": decision, "sha": sha, "by": actor[:64],
                                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "note": " ".join((note or "").split())[:NOTE_MAX]}
    business_settings.save(db, SETTING_KEY, current, actor[:64])
    business_settings.clear_cache()
    return current.get(str(number), {})


def section_of(change: dict, decision: dict | None) -> str:
    if decision and decision.get("decision") == "approved":
        return "approved" if decision.get("sha") == change["sha"] else "changed"
    if decision and decision.get("decision") == "not_now":
        return "not_now"
    if change["checks"] == "failing":
        return "failing"
    if change["checks"] == "running":
        return "running"
    return "ready"


def grouped(changes: list[dict], saved: dict) -> list[dict]:
    """Page sections in order, each with its changes; empty sections are left out."""
    buckets = {key: [] for key, _, _ in SECTIONS}
    for change in changes:
        decision = saved.get(str(change["number"]))
        buckets[section_of(change, decision)].append(dict(change, decision=decision))
    return [{"key": key, "title": title, "help": help_text, "changes": buckets[key]}
            for key, title, help_text in SECTIONS if buckets[key]]


def decision_list(limit: int = 50) -> list[dict]:
    """The owner's saved decisions, newest first, for the assistant connector. Read only."""
    rows = [{"number": int(k), **{f: v.get(f) for f in ("decision", "sha", "at", "note")}}
            for k, v in decisions().items() if str(k).isdigit() and isinstance(v, dict)]
    return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)[:limit]
