"""Every address a customer sees starts with the shop's own domain, never a railway.app host.

The owner's rule, Nov 24: all customers see an imperialautophx.com link. imperialautophx.com and
www.imperialautophx.com point at the same dashboard app, so /status, /sign and invoice pages work there.
PUBLIC_BASE_URL can still name another address of ours, but a railway.app value is ignored.
"""
import os

SHOP_DOMAIN = "https://imperialautophx.com"


def base_url() -> str:
    configured = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not configured or "railway.app" in configured.lower():
        return SHOP_DOMAIN
    return configured


def public_url(path: str) -> str:
    return base_url() + ("" if path.startswith("/") else "/") + path
