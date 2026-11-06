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
| R01 | Public site | Replacement public website (auto, diesel, mobile, roadside, fleet, NVZN) | tested | PR 52 preview under `/site`, not deployed. Home, services, towing, rentals, pricing, FAQ, status and contact pass walkthrough with made-up data. Rentals needs real rates and terms. Pricing needs final prices. No cutover |
| R02 | Public site | Online booking for approved standard services | tested | PR 52, not deployed. Six services approved subject to prices and times. Instant confirmed booking, full duration plus buffer, mobile fee and travel tested with made-up prices. Forms remain off. Needs final prices, durations, ZIP fees and published times |
| R03 | Public site | Complex estimate requests and customer intake with VIN | tested | PR 52, not deployed. Estimate requests create records, including towing, roadside, rentals and NVZN. No owner alert yet, so requests wait in intake until seen |
| R04 | Customer | Secure portal and status links | partial | Tokenized status page works. PR 52 adds lookup by reference and phone, not deployed. No login portal |
| R05 | Customer | Drawn signatures | tested | Walkthrough: signature on phone and desktop |
| R06 | Customer | Line-item estimate approvals | proposed | Signature approves the whole order |
| R07 | Customer | Inspection photos and progress | proposed | No data model |
| R08 | Records | One customer and vehicle record | partial | Pages work. Quo contacts and Operations are not unified |
| R09 | Jobs | Repair orders and line items | tested | Walkthrough |
| R10 | Jobs | Estimates | partial | AI estimate page exists. Full flow still to test |
| R11 | Jobs | Invoices | tested | Create was broken and is fixed on `claude/launch-audit` |
| R12 | Jobs | Parts taxed at 9.2%, labor itemized and untaxed | tested | ADOR June 2026 table and TPR 93-31. Accountant to confirm city labor treatment, sublet and shop fees. Older code charged 8.75% on all items |
| R13 | Scheduling | Booking, buffers, conflicts on PostgreSQL | tested | Walkthrough and PostgreSQL tests |
| R14 | Scheduling | Calendar reading | partial | Apple linked. Google link not provided. No calendar writes |
| R15 | Quo | Message and call intake, alerts, drafts | deployed | Live since Nov 3. `YES` and FYI seen working |
| R16 | Quo | AI call answering | deployed | Sona live. Owner must confirm rules were added |
| R17 | Owner SMS | YES, NO, EDIT, DONE, LATER, OLD | deployed | `YES` verified. Others unproven live |
| R18 | Owner SMS | One short text per item and plain-English replies | approved | Owner decision. Not built |
| R19 | Imperial AI | Job files for each customer | deployed | Texts done through OpenAI. Calls not yet covered, and not proven on real messages |
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
| R35 | Security | Passkey login, separate accounts, approval levels, STOP ALL, automatic backups | in progress | Two-step login, STOP ALL, nightly backups, backup failure alerts, and staff logins with role limits deployed. Off-site copy and owner's live checks remain. Production stays read-only until done |

## Decisions
| Date | Decision | Status |
|---|---|---|
| 2026-11-03 | Automatic customer replies are off until drafts prove reliable | current |
| 2026-11-03 | Sona answers calls, and the owner does not take calls directly | current |
| 2026-11-04 | One text per follow-up item, plain-English replies | current, not built |
| 2026-11-05 | Payments Hub is the trial processor | current |
| 2026-11-05 | The assistant is named Imperial | current |
| 2026-11-05 | Finished work waits for owner approval before merging to main | replaced 2026-11-05: tested work goes live and the owner is told after. Ask first for deletion, public website changes, or new cost |
| 2026-11-05 | Login roles: tech for jobs and calendar, office for customers, jobs, invoices and queue, admin for all but Finance and logins. Finance and logins are owner-only | current |
| 2026-11-05 | Shop address is 7915 N Glen Harbor Blvd, Suite 508, Glendale, AZ 85307 | current |
| 2026-11-05 | Standard services book instantly when a slot fits, complex repairs need an estimate and approval. Keep the one-hour shop buffer and add mobile travel and ZIP-based fees. Exact prices for predictable work, starting prices for variable work | current |
| 2026-11-05 | Saturday is 12 to 5 and open to online booking if the service and buffer fit. Sunday is closed except qualifying emergency roadside service. Towing stays under Imperial Auto Care with staff-confirmed dispatch. Only verified prices are published | current |
| 2026-11-05 | Owner texts PAUSE ALL or STOP ALL to stop automated texting, RESUME to restart | current |

## Completion log
| Date | Change | Evidence | Deployed |
|---|---|---|---|
| 2026-11-05 | Walkthrough on made-up data, PostgreSQL, phone and desktop | `.github/workflows/app-walkthrough.yml` | No |
| 2026-11-05 | Invoice creation, tax, dashboard replies, phone layouts | Branch `claude/launch-audit` | No |
| 2026-11-05 | Owner account claimed, setup code removed, two-step login on | Railway logs `mfa_enabled user=1`, setup variable gone | Yes |
| 2026-11-05 | STOP ALL, RESUME and dashboard stop button | PR 47, automated tests passed. Not yet tested by real text | Yes |
| 2026-11-05 | Nightly backups of both databases at 4:17 AM Phoenix, 14 kept, restored and compared each run | First run: web 6/6/1/32, messaging 434/2376, both matched after restore | Yes |
| 2026-11-05 | Older text intake off by default, failed-backup alerts | PR 49, automated tests and 40 of 40 walkthrough flows | Yes |
| 2026-11-05 | Staff logins and role limits, with `users` columns added after a verified backup | PR 50, automated tests and 46 of 46 walkthrough flows on made-up accounts, counts unchanged at 6/6/1/32 | Yes |
| 2026-11-05 | Public website preview, online booking and estimate requests under `/site` (R01, R02, R03), made-up data, no customer messages | PR 52: regression checks run 42170496534 (167 unit tests, 9 appointment and 6 public booking PostgreSQL tests passed); walkthrough run 42170483672, public booking and estimate passed on phone and desktop, no overflow | No |
| 2026-11-05 | PR 52 corrections: duration enforcement, instant booking, mobile fees and travel, pricing, rentals, FAQ, towing, status lookup, router moved to `app/main.py` | Automated tests, PostgreSQL booking tests and phone and desktop walkthrough passed with made-up data | No |
