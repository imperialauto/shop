"""Jinja2 templates that accept both the old call style, TemplateResponse(name, context),
and the current one, TemplateResponse(request, name, context). Lets the app run on current,
patched Starlette without rewriting every route at once."""
from fastapi.templating import Jinja2Templates as _Base


class Jinja2Templates(_Base):
    def TemplateResponse(self, *args, **kwargs):  # noqa: N802 - Starlette's name
        if args and isinstance(args[0], str):
            name, rest = args[0], args[1:]
            context = rest[0] if rest else kwargs.pop("context", {})
            request = (context or {}).get("request") or kwargs.pop("request", None)
            return super().TemplateResponse(request, name, context, *rest[1:], **kwargs)
        return super().TemplateResponse(*args, **kwargs)
