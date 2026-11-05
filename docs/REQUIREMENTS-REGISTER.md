# Imperial Operations master register

The one permanent record of requirements, backlog, decisions and verified completion. It moves into the Imperial Knowledge Center once that exists.
Add to it. Do not delete earlier entries. Update `Status` only with evidence in the completion log.

The AI assistant is named **Imperial**.

**Status values:** proposed, approved, partial, broken, in progress, implemented, tested, deployed, verified, blocked.
`deployed` means live in production. `verified` means proven on real production use.

## Rules in force
- Extend the existing app. Never create a separate one, and reuse existing work first.
- Preserve all customer, vehicle, financial, booking and repair-order data.
- Owner approval is required for production changes, destructive changes, public website cutover and newly billable services.
- No customer messages during testing. Tests use made-up data.
- Nothing is called complete without evidence.
- `imperialautophx.com` stays on Squarespace until replacement acceptance tests pass and the owner approves cutover.

## Requirements
| ID | Area | Requirement | Status | Evidence or gap |
|---|---|---|---|---|
| R01 | Public site | Replacement public website (auto, diesel, mobile, roadside, fleet, NVZN) | proposed | Not started. Squarespace remains live |
| R02 | Public site | Online booking for approved standard services | proposed | Staff booking only |
| R03 | Public site | Complex estimate requests and customer intake with VIN | partial | Unmerged guided intake on `feature/guided-customer-intake`, for staff form |
| R04 | Customer | Secure portal and status links | partial | Tokenized status page works in walkthrough. No login portal |
| R05 | Customer | Drawn signatures | tested | Walkthrough: signature on phone and desktop |
| R06 | Customer | Line-item estimate approvals | proposed | Signature approves the whole order |
| R07 | Customer | Inspection photos and progress | proposed | No data model |
| R08 | Records | One customer and vehicle record | partial | Pages work. Quo contacts and Operations are not unified |
| R09 | Jobs | Repair orders and line items | tested | Walkthrough |
| R10 | Jobs | Estimates | partial | AI estimate page exists. Full flow still to test |
| R11 | Jobs | Invoices | tested | Create was broken and is fixed on `claude/launch-audit` |
| R12 | Jobs | Parts-only 8.6% tax | tested | Needs owner confirmation of rate. Older code charged 8.75% on all items |
| R13 | Scheduling | Booking, buffers, conflicts on PostgreSQL | tested | Walkthrough and PostgreSQL tests |
| R14 | Scheduling | Calendar reading | partial | Apple linked. Google link not provided. No calendar writes |
| R15 | Quo | Message and call intake, alerts, drafts | deployed | Live since Nov 3. `YES` and FYI seen working |
| R16 | Quo | AI call answering | deployed | Sona live. Owner must confirm rules were added |
| R17 | Owner SMS | YES, NO, EDIT, DONE, LATER, OLD | deployed | `YES` verified. Others unproven live |
| R18 | Owner SMS | One short text per item and plain-English replies | approved | Owner decision. Not built |
| R19 | Imperial AI | Job files for each customer | approved | Blocked on `OPENAI_API_KEY` or Anthropic credits |
| R20 | Imperial AI | Dashboard and SMS commands that take audited actions | proposed | Assistant is read-only |
| R21 | Tasks | Unified task and project engine | proposed | Dispatch queue only |
| R22 | Staff | Employee mobile portal, roles and delegation | partial | Mobile layout and tech finance block tested. No assignments portal |
| R23 | Staff | Inspections, photos, timers | proposed | No data model |
| R24 | Finance | Financial Command Center | partial | Owner view on main. Fuller module in PR 38, unmerged and overlapping |
| R25 | Finance | 2023-2026 reconciliation | in progress | PR 38 import path. Exceptions need owner review |
| R26 | Finance | $500 daily target, job profit, investor reports | proposed | |
| R27 | Alerts | Centralized Alert Center with editable rules and delivery history | partial | Owner SMS history exists. No center, rules or staff channel |
| R28 | Settings | Editable Settings Center | partial | Password and dispatch policies only |
| R29 | Knowledge | Knowledge Center, inbox, decisions, memory | proposed | Acceptance tests in the owner's requirement |
| R30 | Payments | Payment processing | blocked | Payments Hub trial. Waiting on rep answers |
| R31 | Divisions | Fleet, rentals, roadside | partial | Fleet customer type only |
| R32 | Divisions | NVZN Customs | partial | Model on `feature/nvzn-customs-division`, unmerged |
| R33 | Channels | Instagram, Messenger, TikTok | proposed | |
| R34 | Other | Document generation, marketing, inventory, warranties | proposed | PDF estimates exist |
| R35 | Security | Passkey login, separate accounts, approval levels, STOP ALL, automatic backups | proposed | Production stays read-only until done |

## Decisions
| Date | Decision | Status |
|---|---|---|
| 2026-11-03 | Automatic customer replies are off until drafts prove reliable | current |
| 2026-11-03 | Sona answers calls, and the owner does not take calls directly | current |
| 2026-11-04 | One text per follow-up item, plain-English replies | current, not built |
| 2026-11-05 | Payments Hub is the trial processor | current |
| 2026-11-05 | The assistant is named Imperial | current |
| 2026-11-05 | Finished work waits for owner approval before merging to main | current |

## Completion log
| Date | Change | Evidence | Deployed |
|---|---|---|---|
| 2026-11-05 | Walkthrough on made-up data, PostgreSQL, phone and desktop | `.github/workflows/app-walkthrough.yml` | No |
| 2026-11-05 | Invoice creation, tax, dashboard replies, phone layouts | Branch `claude/launch-audit` | No |
