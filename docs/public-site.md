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
| `/site/estimate` | Estimate request with VIN for complex, mobile or roadside work (R03) |

All business details and service wording are in `app/public_site.py` so they can be reviewed in one place.

## What a booking does
- The visitor picks an approved service and one of the open times staff publish on **Live control** (`/live/control`, the existing `booking_slots`).
- It creates a customer (or reuses one only when phone and last name both match), a vehicle, a repair order in `intake`, and a **tentative** shop appointment with the normal buffer after it.
- It uses the same PostgreSQL lock and conflict check as the staff booking API, so two people cannot take one time.
- Staff confirm or cancel it from the existing appointment tools. **Nothing is texted, emailed or called.** The repair order says so.

An estimate request creates the same customer, vehicle and intake repair order, with no appointment.

## Switches (Railway variables)
| Variable | Default | Effect |
|---|---|---|
| `PUBLIC_FORMS_ENABLED` | off | Booking and estimate forms accept submissions only when `true` and the app is not read-only |
| `PUBLIC_BOOKING_SERVICES` | all proposed | Comma list of service slugs the owner approved, e.g. `diagnostic,oil-service` |
| `PUBLIC_BOOKING_LEAD_MINUTES` | 120 | How soon an open time can be booked |
| `PUBLIC_SITE_INDEXABLE` | off | Keeps pages out of Google (`noindex`) until cutover so Squarespace keeps its ranking |
| `PUBLIC_SITE_STREET` | Suite 508 | Street line, until the owner confirms the suite |

Online booking also needs `SHOP_HOURS_JSON` and PostgreSQL. Without them the page says to call or text.

## Owner decisions before cutover
1. Confirm the suite number (records disagree: 417 and 508), phone, email and hours.
2. Approve the list of online-bookable services and their wording.
3. Review all page copy.
4. Approve turning on `PUBLIC_FORMS_ENABLED` in production (production is read-only today, so forms stay closed regardless).

## Cutover steps (owner approval required, not done)
1. Acceptance: walkthrough passes on phone and desktop, PostgreSQL booking tests pass, owner books a test time on staging and confirms it.
2. In `app/main.py`, include `public_site.router` directly, and remove the include at the end of `app/routes/account_security.py`. Decide whether `/` shows the public home page for signed-out visitors.
3. Add redirects from the Squarespace URLs (services, pricing, rentals, FAQ, contact) to the new pages.
4. Set `PUBLIC_SITE_INDEXABLE=true`, add `robots.txt` and a sitemap, then point the domain.
5. Update Google Business Profile and Search Console.
