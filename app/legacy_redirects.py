"""Old Squarespace addresses keep working after the imperialautophx.com cutover.

Each page in the Squarespace sitemap (checked Nov 14, 2026) sends visitors and search engines to
the matching page on the new site with a permanent (301) redirect, so links in Google, texts and
old posts don't land on an error page. Add a line here if an old link turns up later.
"""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

LEGACY_PATHS = {
    "/home": "/site/",
    "/towing": "/site/services/towing",
    "/contact": "/site/contact",
    "/ourwork": "/site/our-work",
    "/ford-dps6-clutch-replacement-phoenix": "/site/services/dps6-transmission",
    "/store": "/site/services",
    "/store/p/mobile-auto-heavy-duty-diesel-roadside": "/site/services/mobile-roadside",
    # Top-level Squarespace nav pages that were not in its sitemap (added Nov 22, 2026).
    "/pricing": "/site/pricing",
    "/faq": "/site/faq",
    "/services": "/site/services",
    "/shop-transport": "/site/services/towing",
    # Rentals is hidden on the new site for now, so its old link goes to the home page.
    "/rentals": "/site/",
}

router = APIRouter(include_in_schema=False)


def _redirect(target: str):
    def go():
        return RedirectResponse(target, status_code=301)
    return go


for old, new in LEGACY_PATHS.items():
    for path in (old, old + "/"):
        router.add_api_route(path, _redirect(new), methods=["GET", "HEAD"])
