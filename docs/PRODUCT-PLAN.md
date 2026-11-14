# Imperial Operations — long-term product plan

Owner direction, Nov 14, 2026. Build for Imperial Auto Care first. Later, remove Imperial's business details and sell it to other mechanics, starting with mobile mechanics and 1–3 bay shops. Nothing in this file changes the launch order: launch work comes first (see REQUIREMENTS-REGISTER.md).

## The pain point we sell
Customer intake → getting the vehicle to the shop → intake and handoff steps once it's there. That is the owner's daily pain today, and it is what the product should solve first: fast first reply, missed-call text-back, follow-ups that don't slip, booking/tow to the shop, and a clean intake (customer, vehicle, concern, photos, approval) with the next step always clear.

## Research summary (Nov 14, 2026)
Full report: `docs/research/2026-11-14-productize-mobile-mechanics.md`.
- Market: roughly 270,000 US independent repair shops, most with 1–5 people; mobile mechanics are a fast-growing, under-served slice.
- Incumbents (Tekmetric, Shopmonkey, AutoLeap, Shop-Ware) charge about $179–$499/month and target multi-bay shops. Mobile-focused tools sit around $30–$60 (ARI). Little sits in the $50–$120 range with strong communication automation.
- AI phone answering is becoming standard (AutoLeap, Jobber, Quo, Tekmetric early access, Nov 2026), so it is not a differentiator by itself.
- Our gap: replies drafted in the owner's own voice and approved with one tap, missed-call and hang-up text-back, a phone-first to-do/waiting/done tracker with hand-off links, and built-in towing/roadside/diesel/fleet.
- Suggested price to test: $49/month solo, $99/month small shop with AI texting included up to a cap.
- First steps: run it at Imperial for 3–6 months and measure (missed calls recovered, reply time, estimates approved); then a 3–5 mechanic pilot outside Glendale before any sales spend.

## Decide now so selling later is cheap (don't over-build)
1. Every table can carry a tenant (shop) id; Imperial is shop #1.
2. No hard-coded business info: name, address, hours, rates, tax, branding, AI voice profile, phone numbers, website copy come from settings.
3. Phone/SMS behind one internal interface (Quo today; Twilio/Telnyx possible later). Credentials per shop.
4. Consent and audit log: how each customer opted in, every AI draft and who approved it, call-recording notice.
5. AI texts are human-approved by default; auto-send only for safe templates the owner turns on.
6. Data export (CSV) and nightly backups.
7. Count usage per shop (texts, AI calls, AI tokens) even before billing.

Not yet: self-serve billing, native iOS/Android apps (fast mobile web app is enough), labor-guide licensing, parts ordering, inventory, payroll, SOC 2, selling the local diagnostic AI.

## Biggest risks
Support load taking time from the shop; TCPA/10DLC compliance (texting registration per shop); dependence on Quo's API and pricing; incumbents adding owner-voice drafting; high churn among very small businesses. Get a telecom/TCPA attorney to review consent and recording before charging other businesses.

## Marketing
The customer-facing website can later carry a page promoting the software. Owner's TikTok/YouTube "how I run my shop" content is the cheapest, most credible channel.

## Idea backlog (not scheduled — do not build before launch)
| Date | Idea | Notes |
|---|---|---|
| 2026-11-14 | Rentals integration for private rental car companies | Manage incoming rental leads, bookings and vehicle status; a possible second product line. Rentals are hidden on the Imperial site until rates and terms exist |
| 2026-11-14 | AI vinyl wrap design generator | Show customers wrap concepts on their own vehicle; fits NVZN Customs once NVZN is active |
| 2026-11-14 | "Time to go home" nudge | Tracker tells the owner when the day's must-do work is done and what can safely wait, so office work doesn't pile up for evenings. Pairs with the daily briefing and an end-of-day wrap-up |
| 2026-11-14 | Master task tracker (mobile) | Design in `docs/TRACKER-DESIGN.md` |
