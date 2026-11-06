# 2023–2026 reconciliation worklist (R25)

This repository is public. **No account numbers, balances, amounts, customer names or file inventories go in this file.**
Those stay in the owner's files and in the `fin_*` tables. This page holds only the method and the status of each step.

Anything not proven by a bank statement that ties is marked **unverified**. A month with no records is shown as
**missing**, never as zero.

## Method
1. **List the accounts.** Every business, personal and mixed account that touched shop money in 2023–2026, by institution
   and last four digits only. Load it as the account list CSV (`account_last4,institution,name,designation,status`).
2. **Collect statements.** For each account, one bank statement PDF per month, January 2023 to the latest closed month.
3. **Record the statement controls.** From each statement's summary box: beginning balance, deposits, withdrawals and
   ending balance (`account_last4,period_start,period_end,beginning,deposits,withdrawals,ending,source_file`).
4. **Load the statement lines.** Every transaction row on the statement
   (`account_last4,date,signed_amount,description,source_file`). Money in is positive.
5. **Tie each month.** Finances › Reconciliation shows *Tied* only when the lines sum to the bank's own deposits and
   withdrawals and beginning + deposits − withdrawals = ending. Fix any *Out of balance* month before moving on.
6. **Fill gaps with OCR or bank exports, marked unverified.** These never count in default reports. Inside a tied month
   they're marked *superseded*, never deleted.
7. **Match transfers.** Moves between the owner's own accounts are matched and kept out of revenue and expense.
8. **Answer the question queue.** Peer payments, cash deposits, Amazon orders, financing and large tool purchases need
   the owner's answer. Use "Apply to all containing…" for repeating payers or merchants.
9. **Close the month.** A month is closed only when every in-scope business account is tied for it.
10. **Add the processors.** Square, Stripe, PayPal, SwipeSimple and Apple Cash payouts are matched to bank deposits
    (not built yet: no importer for processor reports, so these stay unverified bank deposits).

## Status
Updated 2026-11-05. Production was not touched to produce this.

| Step | Status | Evidence or gap |
|---|---|---|
| Import, tie, dedupe and transfer logic | built, CI-tested on invented data | PR 38, combined with the shop payments page on `claude/finance-combine-reconciliation` |
| `fin_*` tables in production | not done | `migrations/20261104_finance.sql` must run after a verified backup. Owner approval required |
| Imports in production | blocked | Recovery mode blocks every finance POST. Needs recovery mode off, or a `/finance/` exemption in `app/main.py`, which another change owns |
| 1. Account list | not started | Owner to confirm which accounts were used, and which were business or personal |
| 2. Statements, 2023 | missing | No 2023 statements found in the owner's Google Drive "Bank Statements" folder |
| 2. Statements, 2024 | missing | None found there |
| 2. Statements, 2025 | missing | None found there |
| 2. Statements, 2026 | partial, unverified | Several months found there for two institutions. Not yet opened, matched to accounts or checked for duplicates |
| 3–9 | not started | Waits on steps 1–2 and the migration |
| 10. Processor payouts | not built | No processor import. Stripe is reachable by Claude read-only, not by the app |

"Found in Drive" means a file with a statement-like name exists. It has not been read, so its account, period and
completeness are **unverified**.
