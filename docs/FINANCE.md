# Finances module

Owner-only section at `/finance/`. No financial data belongs in this repository: it is public.
Bundles of real records stay out of Git and are loaded through the owner's Import tab.

## Deploy order
1. Back up the shop database and verify a restore.
2. Apply `migrations/20261104_finance.sql` (rollback: `.down.sql`). It only adds `fin_*` tables.
   `/readyz` reports not ready until this is done.
3. Set `FINANCE_OWNER_USERNAMES` on `web` to the owner's login. Empty means nobody can open Finances.
4. Merge. `main` auto-deploys. Sign out and back in so the Finances link appears.
5. On the Import tab upload the account list first, then private rules, statement controls, statement lines and OCR trace.

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
