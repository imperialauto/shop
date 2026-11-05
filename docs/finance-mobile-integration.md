# Imperial Financial Command Center — mobile integration

## Verified application architecture
- Code: `imperialauto/shop`, FastAPI/Jinja2, PostgreSQL via Railway.
- UI: existing `app/templates/base.html` mobile header, sidebar and shared cards.
- Access: `get_current_user` + `require_admin`. Service advisors and technicians must **not** see the finance route or link.
- Existing operational data: `Invoice`, `Payment`, `RepairOrder`. These are not the complete historical bank ledger.
- Recovery: current application read-only mode blocks non-GET actions except allowlisted internal communications; this change must not bypass it.
- This branch adds `/finance/` and a responsive, read-only view of recorded operational payments. It does not modify existing tables or seed private financial records.

## What must come next to make the portfolio live
**Do not upload the actual financial portfolio to this public GitHub repository.** The private transaction evidence, account IDs, source PDFs, payroll and taxes belong in authenticated encrypted storage, not public code or static assets.

1. Run a production-backup and schema audit; identify the canonical PostgreSQL service, owner role, and restore procedure before migrating.
2. Add an **additive**, reversible finance schema using a reviewed migration:
   - `finance_source_files`: provider, account, original filename, SHA-256, period start/end, content reference, import status and parser version.
   - `finance_entries`: unique `source_system + source_account + external_transaction_id` where available, normalized posted date, signed amount (integer cents), currency, raw description, payee, document page reference and raw-value JSON. No deletion or mutation of original records.
   - `finance_classifications`: one immutable revision per decision, type `revenue / cost_of_sales / operating_expense / asset / transfer / refund / personal / pending`, reviewer, source, confidence, business purpose and effective time.
   - `finance_matches`: linked transactions and matching rule, including fee differences, partial settlements, transfer source/target and verification state.
   - `finance_audit_log`: imports, classification approvals, overrides, exports and user.
3. Build admin-only CSV imports with strict size/type/schema validation, malware scanning where available, idempotent source hashes and import previews. Never treat an OCR candidate as a verified bank record until checked against its statement. Keep each upload atomic.
4. Establish exact source/account coverage and monthly statement balance tie-outs before any "verified" badge.
5. Match original customer receipts to their PayPal, Square, SwipeSimple, Zelle, Apple Cash and bank payouts; treat tax collected and third-party tips separately; do not count internal transfers or payout receipts as new sales.
6. Compute P&L only from reviewed economic events, with dates and accounting basis visible. Show separate **confirmed** and **unresolved** amounts; provide source-to-ledger drill-down.
7. Add mobile sections within the same `/finance/` workspace: Overview, Transactions, Reconciliation, Monthly P&L, Tax Reserve and Investor Cash Flow. Prefer tabs and vertical transaction cards on phones, not extra top-level navigation screens.
8. Use the existing Operations `Invoice` and `Payment` IDs for future operational-to-bank matches. Never assume their totals equal revenue across all business accounts.
9. Add controls before enabling uploads: owner-only access, CSRF checks for mutations, rate limiting, backups, audit trail, and no financial values in client-visible error logs.
10. Roll out via review branch, isolated QA database, owner test, limited release, then production after explicit approval. Do not auto-deploy this PR.

## Source inventory (available to reconciliation outside the app)
- Bank statement files: Chase batches 1–4, older recovered Chase business statement, final BofA statements.
- Consolidated financial portfolio workbook and 2023–26 raw/OCR evidence.
- Square merchant report for 2023-09-01 through 2025-11-27 (aggregate sales **not** individual settlement records).
- Finances linked accounts and stored user-confirmed transaction classification rules.
- QuickBooks current P&L and balance sheet, which are bookkeeping reports rather than completed historical bank audits.

The ChatGPT Finance connector cannot be treated as a drop-in production API for this Railway app. A separately authorized and secured data-import/sync integration is required.

## Mobile acceptance checklist
- On 375px and 430px screens: no horizontal page scrolling, date filter remains usable, readable cards, and payments stack vertically.
- On tablet/desktop: two-panel view and concise KPI row.
- Admin sees Finance in the existing mobile sidebar; technician or customer users do not.
- `GET /finance/` remains read-only and works while the app is in recovery mode.
- No finance data is inserted, altered or deleted by simply visiting the page.
- Pending source imports are labelled pending, never zero revenue or audited.
- The financial P&L and tax estimate cannot go live until imports, classifications and account coverage are verified.
