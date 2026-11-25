"""Jinja2 templates that accept both the old call style, TemplateResponse(name, context),
and the current one, TemplateResponse(request, name, context). Lets the app run on current,
patched Starlette without rewriting every route at once."""
from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates as _Base


def shop_time(value):
    """A saved time (naive values are UTC) as Phoenix time, so 7 PM tonight doesn't read as tomorrow."""
    if not isinstance(value, datetime):
        return value
    from app.appointments import PHOENIX
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(PHOENIX)


def _car_here(ro) -> bool:
    from app import shop_board
    return shop_board.car_here(ro)


class Jinja2Templates(_Base):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from app import public_links
        self.env.globals["public_base"] = public_links.base_url  # customer links: imperialautophx.com
        self.env.filters["shop_time"] = shop_time
        from app import discounts
        self.env.filters["money"] = discounts.money  # -$75.00 for a discount, never $-75.00
        self.env.globals["car_here"] = _car_here  # the job's car is at the shop, as the TV board counts it

    def TemplateResponse(self, *args, **kwargs):  # noqa: N802 - Starlette's name
        if args and isinstance(args[0], str):
            name, rest = args[0], args[1:]
            context = rest[0] if rest else kwargs.pop("context", {})
            request = (context or {}).get("request") or kwargs.pop("request", None)
            return super().TemplateResponse(request, name, context, *rest[1:], **kwargs)
        return super().TemplateResponse(*args, **kwargs)
