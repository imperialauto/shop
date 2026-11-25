"""Jinja2 templates that accept both the old call style, TemplateResponse(name, context),
and the current one, TemplateResponse(request, name, context). Lets the app run on current,
patched Starlette without rewriting every route at once."""
from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates as _Base


def _ask_on() -> bool:
    import os
    return os.getenv("ASK_IMPERIAL_ENABLED", "").strip().lower() == "true"


def _ask_href(request) -> str:
    """Ask Imperial opens with the job selected when used from inside a job."""
    import re
    match = re.match(r"^/ro/(\d+)(?:/|$)", request.url.path) if request is not None else None
    return f"/ask?ro={match.group(1)}" if match else "/ask"


def shop_time(value):
    """A saved time (naive values are UTC) as Phoenix time, so 7 PM tonight doesn't read as tomorrow."""
    if not isinstance(value, datetime):
        return value
    from app.appointments import PHOENIX
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(PHOENIX)


class Jinja2Templates(_Base):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.env.globals.setdefault("ask_on", _ask_on)
        self.env.globals.setdefault("ask_href", _ask_href)
        from app import public_links
        self.env.globals["public_base"] = public_links.base_url  # customer links: imperialautophx.com
        self.env.filters["shop_time"] = shop_time

    def TemplateResponse(self, *args, **kwargs):  # noqa: N802 - Starlette's name
        if args and isinstance(args[0], str):
            name, rest = args[0], args[1:]
            context = rest[0] if rest else kwargs.pop("context", {})
            request = (context or {}).get("request") or kwargs.pop("request", None)
            return super().TemplateResponse(request, name, context, *rest[1:], **kwargs)
        return super().TemplateResponse(*args, **kwargs)
