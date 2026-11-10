# Customer Journey — controlled release
Owner-facing feature inside Imperial Operations at /journey/.

## What it shows
- Anonymous per-tab paths: page, form stage, native validation errors, submission, confirmation.
- Seven- and 30-day funnels for booking, estimates, roadside, status lookup and secure RO approvals.
- Secure status or approval visits are linked to the correct RO by a six-hour signed analytics context; no private status token is persisted.
- Possible pauses are inferred after ten minutes, not proven customer difficulty.
- Preview/QA traffic is separate from live visitors.
- No replay video, keystrokes, field contents, signatures, IP addresses, user-agent strings or raw URLs.
- Respects Global Privacy Control and Do Not Track. Previous visits cannot be recovered retroactively.

## Approval-gated setup
Tracking is OFF by default. It requires the additive migration migrations/20261110_journey_events.sql and CUSTOMER_JOURNEY_ENABLED=true. Existing recovery read-only restrictions are preserved.
1. Verify current database backup and documented restore plan before modifying production schema.
2. Apply the additive migration in disposable QA PostgreSQL and inspect the new table and indexes.
3. Enable flag in QA only; test synthetic booking with validation failure, completion and synthetic RO approval.
4. Confirm RO association, privacy and usable flows when tracking collection fails.
5. After explicit owner approval, apply the production migration and enable the feature. Keep Squarespace live, public replacement forms OFF and domain unchanged until separately approved.

## Risks and mitigations
- Additional DB writes/storage: bounded payloads (1,024 bytes), 200 events per tab/day; remove events older than 30 days during ingestion.
- Nonlinear browsing and blocked JS can create false-positive dropoff signals; report marks them as possible.
- Missing database schema: collector fails closed, page remains disabled, existing site functions unaffected.
- Avoid collection of sensitive input, tracking cookies, long-term identifiers or private status tokens.

## Rollback
1. Disable CUSTOMER_JOURNEY_ENABLED immediately (no schema change needed).
2. Keep the inert optional table for reversible recovery.
3. Only with explicit approval, use migrations/20261110_journey_events.down.sql to DROP the analytics table. This is destructive to analytics history, not customer/RO records.
4. Roll back code separately on deployment errors.

## Acceptance
- Owner-only reporting; technician/office visitors denied.
- Job #7 style secure link logs: opened status, review click, approval opened, form step and actual completed signature (without signature data).
- Booking and estimate QA funnels show view/start/submit/error/complete stages; error recovery causes no duplicate customer messaging.
- No public website forms enabled or public domain switched.
