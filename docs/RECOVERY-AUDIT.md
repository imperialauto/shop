# Imperial production recovery audit — October 26, 2026

## Verified starting state
- Repository main: 7b6d02125061dd49716adcaf18b91c8919eb0e54.
- Railway respectful-charm / production: web CRASHED; shop Node messaging SUCCESS; both PostgreSQL deployments SUCCESS with persistent volumes.
- Web fails during Python import: app/routes/repair_orders.py line 46, malformed try/except indentation. The board addition also omitted its template.
- Source compile and template checks plus disposable SQLite tests establish local behavior, not live production workflow completion.

## Data lineage — preserve all stores
| Store | Evidence | Role / unresolved checks |
|---|---|---|
| Postgres-Tvkp | Node startup explicitly reports postgres-tvkp.railway.internal:5432, database railway, schema public. | Confirmed messaging Prisma connection; do not assume this is the shop's authoritative customer/RO database. Runtime read/write test messages were already stored here. |
| Postgres | Deployment SUCCESS, persistent 5 GB volume. | Web DATABASE_URL is configured but OAuth variable values/references are withheld. Recovery diagnostic now confirms web uses postgres.railway.internal, database railway. Counts: customers 6, vehicles 7, repair_orders 6, line_items 7, estimate_sessions 12, communications 6, users 1; invoices/payments/parts_orders 0. Authoritative for existing Python shop records; not the separate Sites data. Do not repoint or delete either database. |
| Operations Sites D1/R2 | Separate owner-private project appgprj_6ada8262800c8191864e1199944de8d6, customer gateway appgprj_6adce339f8cc819196e4db4dcbac4629. | Recent intake, notes, clocks, attendance and approval implementation lives here, not automatically in this GitHub repository or PostgreSQL. No migration/sync has been established. |
| Quo | Live messages/transcripts accessible through connected service; funded owner test delivered October 26. | Authoritative provider message/delivery history; local caches do not replace provider receipt evidence. |

No database has been reset, merged, moved, repointed or given new credentials. No production schema change or default-account creation is part of recovery startup.

## Recovery changes
- Restore valid repair-order source using the last working file as a baseline, retain the board route/helper, add its missing template.
- Default APP_READ_ONLY=true. Recovery blocks production POST/PUT/PATCH/DELETE actions except login. It does not silently turn old workflows loose against production.
- Startup performs aggregate read-only database diagnostics with PostgreSQL READ ONLY transaction and per-statement timeout. It reports configured host, expected table/column presence and counts, never rows, credentials, hashes or raw connection errors.
- No startup create_all, ALTER TABLE, default admin seeding or follow-up scheduler. Existing schema/users/data are preserved. The legacy default admin password is no longer accepted.
- /healthz verifies the web process; /readyz separately verifies the expected database schema. A successful deployment/liveness response alone does not clear launch.
- Invoice signing requires the RO's unguessable token. Signing no longer marks an invoice paid or automatically starts work. Existing payment records are not modified.
- Recovery mode is visible in the staff shell. Enablement requires a subsequent reviewed release after database and workflow checks, not an automatic timer.

## Workflow audit before restoration
| Workflow | Repository implementation / actual verification limit |
|---|---|
| Customer / vehicle intake | SQLAlchemy CRUD/forms exist; fixture reads render. Production writes blocked pending data audit. Customer-vehicle ownership and input/CSRF checks require further review. |
| Quo messaging | Separate /messaging Node service; read/draft and owner test delivery verified previously. Python internal context/mirror endpoints were unavailable while web crashed. Do not claim complete real-time synchronization or cross-vehicle matching. |
| Booking | Python Google Calendar helper and SMS intake path exist; credentials for Google are not configured on web in the inspected variable inventory. Sites has a separate request/queue implementation. Neither constitutes a verified end-to-end provider booking in this recovery. |
| Estimates | Legacy generation/form/PDF routes exist; fixture estimate page renders. Source-verified supplier/labor retrieval and safe reviewed estimate conversion are not cleared. |
| Approvals | Legacy whole-RO drawing signature lacks revision-bound individual line decisions. Numeric invoice exposure/payment-on-signature bug repaired. Newer line-choice/typed-signature code is separately deployed privately in Sites; public gateway draft version 7 is saved, not released. Its hosted signature journey remains unfinished testing at handoff. |
| Repair orders | Read screens and repaired board render against fixtures; workflow mutations remain paused. |
| Technician timers / attendance | Not implemented in this Python repository. Separate job/shift clocks exist in Sites. No complete PostgreSQL integration/efficiency reporting. |
| Inspections/photos | No complete DVI/photo storage workflow in this Python repository. Sites has separate partial inspection/file support; real hosted attachment path still not fully verified. |
| Customer status | Legacy token-scoped status reads render in fixtures. Separate public Sites token status and manual delivered link were verified. These records/tokens are not interchangeable. |
| Invoicing/payments | Legacy invoice/payment rows exist. Signature must never establish payment. Manual payment input accepts insufficiently validated amounts and has no provider receipt reconciliation; production writes remain paused. |
| NVZN | Separate Sites catalog/reference/request choices exist; Python project has no finished NVZN division flow. No automatic replication of the service menu. |

## Requested next scope — not completed by crash repair
1. Establish system-of-record ownership from actual schemas, record freshness and references; reconcile existing serviced customers before any outreach. Back up and verify restore before migrations or merging stores.
2. Carry the existing working features into one operational workspace without losing Quo/provider IDs, customer/vehicle matching, approvals, documents or clocks. Determine a reviewed migration/sync plan rather than operating two untracked dashboards.
3. Dashboard-managed booking specials: approved prices/scope, eligibility, availability and start/end dates; explicit publication review; selection carried into the saved request and estimate without retroactively changing old quotes.
4. $500/day collected-revenue target in Phoenix time: count actual recorded/verified receipts, distinguish provider-verified from manually recorded collections, subtract refunds/voids, prevent duplicate receipts; never use open estimates, invoices marked paid, signatures, or profit as substitutes for collected cash.
5. Full public navigation/service pages, booking, fleets and gallery. User's original unaltered photos only; curate assets with source/permission records. No generated or altered business-work photos. Retain Squarespace until the replacement is verified and approved.
6. Complete task assistant, VIN capture, source-backed parts/labor lookup, low-volume action-triggered texts, post-service/review follow-up, deposits, provider bookings, final invoices and accounting handoff with per-feature end-to-end evidence.

## Local validation
- Five unittest groups: non-mutating startup; existing authenticated/customer token read screens; recovery write/outbound blocks; invoice token scope and signature-vs-payment separation; credential-free read-only audit.
- All Python modules compile and all Jinja templates compile.
- Tests use only an explicit disposable SQLite database. They do not assert PostgreSQL identity, production login, provider calls or complete launch readiness.

## Production recovery verification
- PR7 merged to main as 52c10725802702a780e30e1ca9e9341025225aa5.
- Web deployment 3dff9d86-6e14-4da3-b1d7-c618d1d8d6ba SUCCESS; messaging deployment 6882f807-0bc5-47ee-acd5-a9b731c8eaa4 SUCCESS.
- Live HTTP GET /healthz and /readyz both 200, mode read-only; database connected. /login renders; unauthenticated / redirects to login.
- Web database audit: connected true, no missing tables or columns. Node startup: database already in sync with Prisma schema, listener started.
- No authenticated production session or complete provider/customer workflow has been verified by these health checks. Existing legacy default credentials are rejected; no credentials were rotated or seeded.
- Both PostgreSQL services are in use. Sites remains a third separate operational store. There is no single complete unified system of record yet.
