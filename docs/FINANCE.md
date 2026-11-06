# Finances module

Owner-only section at `/finance/`. No financial data belongs in this repository: it is public.
Bundles of real records stay out of Git and are loaded through the owner's Import tab.

## Who can open what
Everything under `/finance` is owner-only: the claimed owner account (`role = owner`) and nobody else.
`app/permissions.py` lists `/finance` as `OWNER_ONLY`, and each finance route and export repeats the check
through `app/finance_access.py`. Admin, office and technician logins are refused.
- **Shop payments** (the landing tab): payments and invoices already in Imperial Operations. Read-only.
- **Bank ledger** (Overview, Ledger, Reconciliation, Reports, Accounts, Investor Center, Import).

## Deploy order
1. Back up the shop database and verify a restore.
2. Apply `migrations/20261104_finance.sql` (rollback: `.down.sql`). It only adds `fin_*` tables.
   `/readyz` reports not ready until this is done, because the app now expects these tables. Railway's health check
   uses `/healthz`, so the deploy itself is not blocked. Until the migration runs, the ledger tabs show a "not set up yet"
   page and Shop payments keeps working.
3. Merge (owner approval). `main` auto-deploys. The Finance link in the sidebar already exists.
4. Imports are POST requests, so **recovery mode blocks them**. Either turn recovery mode off, or add a `/finance/`
   exemption to `read_only_exempt` in `app/main.py`. That file is owned by the login and permissions change, so the exemption
   is deliberately not in this branch.
5. On the Import tab upload the account list first, then private rules, statement controls, statement lines and OCR trace.
   See `docs/RECONCILIATION-2023-2026.md`.

## Trust levels
- `statement_tied`: from a bank statement whose rows sum to the bank's own totals. Default for all reports.
- `unverified`: OCR or bank export with no tied statement. Shown only when chosen, with a warning.
- OCR rows inside a tied period are marked `superseded`, never deleted.

## Integrations
| Source | State |
|---|---|
| CSV import | Built and idempotent |
| QuickBooks | No API connection in this app. Needs an Intuit app and owner OAuth, or a CSV export |
| Stripe | Connected to Claude read-only, not to this app. Needs a restricted key if wanted live |
| Bank feeds | Not connected. Era Context's plan hides live accounts |
No integration writes to an outside service or moves money.
