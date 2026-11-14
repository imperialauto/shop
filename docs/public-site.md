# Replacement public website (R01, R02, R03)

The new public site lives inside Imperial Operations under `/site`. It does not replace
`imperialautophx.com`. Squarespace stays live until the acceptance tests below pass and the
owner approves cutover.

## Pages
| Path | What it is |
|---|---|
| `/site/` | Home |
| `/site/services`, `/site/services/<slug>` | Auto repair, diesel, European and Sprinter, DPS6, fleet, mobile and roadside, NVZN Customs |
| `/site/contact` | Address, hours, call, text, directions |
| `/site/book` | Online booking for approved standard services (R02) |
| `/site/estimate` | Estimate request with VIN for complex, towing, roadside, rental and NVZN work (R03) |
| `/site/pricing`, `/site/rentals`, `/site/faq` | Rebuilt pages. Rentals needs rates and terms from the owner |
| `/site/status` | Status link lookup by reference and phone |

All business details and service wording are in `app/public_site.py` so they can be reviewed in one place.

## What a booking does
- The visitor picks an approved, priced service, then the shop or their location, then a time.
- Times start at the open times staff publish on **Live control**. A time is offered only if the service's full length fits, plus the buffer, inside shop hours.
- A shop booking holds the service length plus the shop buffer (60 minutes by default). A mobile booking holds the service length plus travel before and after, instead of the buffer.
- It uses the same PostgreSQL lock and conflict check as the staff booking API, so two people cannot take one time.
- It creates or reuses a customer (phone and last name must both match), a vehicle, an `intake` repair order, and a **confirmed** appointment.
- **No text, email or call is sent.** Confirmation and the status link appear on screen. The repair order says so.

Complex repairs, towing, roadside and rentals go through `/site/estimate`, which creates the same records with no appointment. These requests do not alert the owner yet.

## Switches (Railway variables)
| Variable | Default | Effect |
|---|---|---|
| `PUBLIC_FORMS_ENABLED` | off | Forms accept submissions only when `true` and the app is not read-only |
| `PUBLIC_BOOKING_SERVICES` | all six | Comma list of approved service slugs |
| `PUBLIC_SERVICE_CATALOG` | empty | JSON per service: `price`, `price_type` (`exact` or `starting`), `minutes`, `mobile`. A service with no price cannot be booked |
| `PUBLIC_MOBILE_ZONES` | empty | JSON per ZIP code: `fee`, `travel_minutes`. Other ZIP codes get an estimate request |
| `PUBLIC_BOOKING_LEAD_MINUTES` | 120 | How soon a time can be booked |
| `PUBLIC_SITE_INDEXABLE` | off | Keeps pages out of Google until cutover |

Online booking also needs `SHOP_HOURS_JSON` and PostgreSQL. Without them the page says to call or text.

## Cutover steps (owner approval required, not done)
1. Acceptance: walkthrough passes, PostgreSQL booking tests pass, owner tests the preview.
2. Decide whether `/` shows the public home page for signed-out visitors.
3. Add redirects from the Squarespace URLs to the new pages. Done in code (Nov 14, `app/legacy_redirects.py`): all 7 pages in the Squarespace sitemap 301 to their new pages. Both `imperialautophx.com` and `www.imperialautophx.com` must point at the app so old `www` links resolve.
4. Set `PUBLIC_SITE_INDEXABLE=true`, add `robots.txt` and a sitemap, then point the domain.
5. Update Google Business Profile and Search Console.
