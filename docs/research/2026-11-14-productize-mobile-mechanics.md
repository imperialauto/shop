# Research: Imperial Operations as a product for mobile mechanics (Nov 14, 2026)

Condensed from a Claude research report run for the owner on Nov 14, 2026. Prices change often; verify on each vendor's pricing page before quoting. Not legal advice.

## Verdict
Yes, but narrow: productize the communication and follow-up layer for mobile mechanics and 1–3 bay shops at about $49–$99/month. Don't try to replace Tekmetric/Shopmonkey. Validate with a 3–5 mechanic pilot after 3–6 months of measured use at Imperial.

## Market
- ~270,000 US independent repair shops (Auto Care Association 2024; AAPEX 2025), ~967,000 technicians; most businesses have 1–5 people.
- Mobile mechanic market estimates disagree widely (Mordor: $4.27B global 2025, 8.8% CAGR; Dataintelo: $11.4B global). No government industry code tracks mobile operators separately.
- Many small shops still run on paper, QuickBooks or generic invoicing apps.

## Competitors (late 2026, monthly)
| Product | Price | For | AI phone/texting |
|---|---|---|---|
| Tekmetric | $179–$439 | Established shops | Receptionist in early access Nov 2026 (Tekmetric Phones only) |
| Shopmonkey | $239–$499 + $20/user over limit | General shops | Front Desk Concierge (CRM add-on) |
| AutoLeap | $179–$449 | Multi-bay | AIR receptionist $99/mo for 200 calls |
| Shop-Ware | $251–$389 | Premium | AI receptionist |
| Torque360 | $89.99–$179.99 (annual) | Budget | Texting only |
| ARI | $39.99–$59.99 | Mobile mechanics, solo | No AI receptionist |
| Jobber | $29–$499 | Home services, some mobile mechanics | AI Receptionist $29/mo + usage; texts back hang-ups |
| Housecall Pro | $59–$329 | Home services | CSR AI add-on |
| Quo (current phone system) | $15–$47/user | Any small business | Sona: 10 calls/mo included, then paid tiers |

## Pain points (Capterra 2025–2026 reviews)
Too expensive for 1–2 person shops; phone apps are shrunken desktop apps; slow support and hard cancellation; texts not delivered (10DLC); per-user and per-transaction fees stacked on subscriptions.

## Requirements to sell
- Multi-tenant: shop id on every table, scoped queries, per-shop settings and credentials.
- A2P 10DLC texting registration per shop (brand + campaign, ~10–15 day review).
- TCPA: AI voices count as artificial (FCC 24-17); honor STOP immediately; revised opt-out rules (FCC 26-67) effective Nov 25, 2026; consent logging; recording notice.
- Payments through a platform model (e.g. Stripe Connect); never hold other shops' money.
- Labor data (MOTOR, Mitchell 1, ALLDATA) is licensed; don't build it in year one.
- Privacy policy URL (required for new 10DLC campaigns), backups, data export.
- Support and onboarding is the hidden cost; cap the pilot and charge for setup.

## Go-to-market
Owner-built examples: Tekmetric (shop owner Sunil Patel, launched 2017, gave it free in 2018), Shop-Ware (mechanic/shop owner Carolyn Coquillette, 2013). Channels: owner's TikTok/YouTube, mechanic creators, Facebook groups, trade groups, later AAPEX/SEMA. Fallback exit: white-label the AI-texting module.

## Biggest risks
Incumbents adding the same features; support load; TCPA/10DLC exposure; Quo dependence; churn of tiny businesses.

## Key sources
- https://www.autocare.org/news/latest-news/details/2024/04/10/survey-84-of-independent-repair-shops-view-vehicle-data-access-as-top-issue-for-their-business
- https://www.aapexshow.com/blog/automotive-aftermarket-economy/
- https://autoleap.com/air/
- https://www.tekmetric.com/ai-info
- https://www.capterra.com/p/169022/Shopmonkey/reviews/
- https://www.capterra.com/p/201737/ARI/reviews/
- https://www.capterra.com/p/216500/Autoleap/reviews/
- https://www.wsgr.com/en/insights/fcc-rules-ai-generated-voices-are-artificial-under-the-tcpa.html
- https://www.motor.com/products-services/data-products/estimated-work-times/
