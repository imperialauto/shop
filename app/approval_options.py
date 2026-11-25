"""Option groups on an estimate: lines whose description starts "OPTION 1", "OPTION 2", ... are alternatives.
The customer takes one option; lines without a number ("OPTIONAL ...", labor, supplies) go with any option."""
import re

OPTION = re.compile(r"^\s*option\s*#?\s*(\d+)\b\s*(?:\(([^)]{1,60})\))?", re.I)


def group_of(description: str | None) -> str | None:
    match = OPTION.match(description or "")
    return match.group(1) if match else None


def groups(lines) -> list[dict]:
    """[{"number": "1", "name": "AutoZone Duralast parts", "ids": [...], "total": 1012.78}, ...] for 2+ options."""
    found: dict[str, dict] = {}
    for li in lines:
        match = OPTION.match(li.description or "")
        if not match:
            continue
        g = found.setdefault(match.group(1), {"number": match.group(1), "name": None, "ids": [], "total": 0.0})
        g["name"] = g["name"] or (match.group(2) or "").strip() or None
        g["ids"].append(li.id)
        g["total"] = round(g["total"] + (li.total or 0), 2)
    return [found[k] for k in sorted(found, key=int)] if len(found) > 1 else []


def mixed_error(lines, decisions: dict) -> str | None:
    """Approving lines from two different options at once is a mistake the page should catch."""
    taken = {group_of(li.description) for li in lines if decisions.get(li.id) == "approved"} - {None}
    if len(taken) > 1:
        return "Pick one option: approve one option's items and decline the other's."
    return None
