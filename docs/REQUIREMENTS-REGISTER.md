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
- **Continuous workflow optimization (permanent, Nov 9):** for every button, link, form, screen and workflow, ask how to make it easier, quicker, more intuitive, or unnecessary. Trace the journey, count taps, screens, entries and decisions, reuse data already on file, make the next action obvious, update every screen at once, and work on phone and desktop. Never remove safeguards, approvals, financial controls or audit history to save a step. Record findings and measured results under R37, not in a separate list.
- **Imperial as an operations manager (permanent, Nov 9):** Imperial manages work proactively, not just displays it. For every workflow ask whether the action can be eliminated, completed from existing data, combined, or prepared before it's asked for; whether routine work can finish without interrupting the owner; whether unfinished work recovers by itself; whether every record updates; how failures are detected and reported; and what happens when Quo, Railway or an AI provider is down. Every important action has an owner, status, completion condition and audit trail. Prefer existing screens over new ones. Safe, reversible, already-authorized admin actions need no extra confirmation. Customer messages, financial commitments and destructive changes always keep their approval. Measure every significant change (taps, screens, entries, errors, recovery). Goal: fewer steps from a customer needing service to the work done and paid. Recorded under R38.

## Requirements
| ID | Area | Requirement | Status | Evidence or gap |
|---|---|---|---|---|
| R01 | Public site | Replacement public website (auto, diesel, mobile, roadside, fleet, NVZN) | deployed | PR 52 merged Nov 8 and live under `/site` with public forms off and hidden from search. Domain still on Squarespace. Rentals need real rates and terms, pricing needs verified prices. Not yet in the shared purple and gold design (R36). No cutover |
| R02 | Public site | Online booking for approved standard services | deployed | PR 52 live with forms off. Instant booking, duration plus one-hour buffer, mobile fee and travel tested on made-up data and in the private preview. Needs final prices, durations and ZIP fees, then owner acceptance before forms are switched on |
| R03 | Public site | Complex estimate requests and customer intake with VIN | deployed | PR 52 live with forms off. Requests create records and an owner queue item. Lead lifecycle work in draft PRs 55, 58 and 59 is not merged |
| R04 | Customer | Secure portal and status links | partial | Tokenized status page and PR 52 lookup by reference and phone are live. No customer login portal |
| R05 | Customer | Drawn signatures | tested | Walkthrough: signature on phone and desktop |
| R06 | Customer | Line-item estimate approvals | deployed | PR 67: customer approves or declines each line; only approved lines are billed. PR 73: the link reaches the customer by text only after the owner's YES. Not yet used on a real job |
| R07 | Customer | Inspection photos and progress | proposed | No data model |
| R08 | Records | One customer and vehicle record | deployed | PR 71 Customer file timeline (live Quo texts and calls, jobs, approvals, visits, invoices, payments, queue items). PR 74 phone search in any format and vehicle list on new jobs. PR 75 owner-reviewed Quo import and automatic customer records for new texters and callers. Import not yet run by the owner; Customer file opened once in production without errors. Not verified against Quo |
| R09 | Jobs | Repair orders and line items | tested | Walkthrough |
| R10 | Jobs | Estimates | partial | PR 66 sets labor from the price sheet and fixes duplicate customers. The AI part has never run in production |
| R11 | Jobs | Invoices | tested | Create fixed in PR 42 (`claude/launch-audit`), merged. No real invoice yet (0 invoices in production) |
| R12 | Jobs | Parts taxed at 9.2%, labor itemized and untaxed | tested | ADOR June 2026 table and TPR 93-31. Accountant to confirm city labor treatment, sublet and shop fees. Older code charged 8.75% on all items |
| R13 | Scheduling | Booking, buffers, conflicts on PostgreSQL | deployed | PostgreSQL tests pass; booking engine live with PR 52, forms off. 0 appointments in production |
| R14 | Scheduling | Calendar reading | partial | Apple linked. Google link not provided. No calendar writes |
| R15 | Quo | Message and call intake, alerts, drafts | deployed | Live since Nov 3; intake runs every few minutes in production logs. PR 75 adds a customer record for each new number |
| R16 | Quo | AI call answering | deployed | Sona live. Owner must confirm rules were added |
| R17 | Owner SMS | YES, NO, EDIT, DONE, LATER, OLD, STOP ALL, RESUME | deployed | YES verified live. PAUSE ALL and RESUME tested by the owner Nov 8. PR 63 stops price questions being treated as LATER. NO, EDIT, DONE, LATER and OLD unproven live |
| R18 | Imperial AI | One short text per item and plain-English replies | deployed | PR 45 live since Nov 5. Plain-English replies not yet proven on real texts |
| R19 | Imperial AI | Job files for each customer | deployed | Texts done through OpenAI. Calls not yet covered, and not proven on real messages |
| R20 | Imperial AI | Dashboard and SMS commands that take audited actions | partial | Audited owner actions exist for queue items, STOP ALL and approval-link sends (PR 73). Project questions by SMS are in draft PR 57, not merged |
| R21 | Tasks | Unified task and project engine | proposed | Dispatch queue, My day and Home summary read the same queue and job records. No separate task records yet. Duplication concern to audit |
| R22 | Staff | Employee mobile portal, roles and delegation | partial | PR 50 roles, PR 65 My day and job assignment live. Logins for Rico (tech) and office coordinator created Nov 8. Not used by staff yet |
| R23 | Staff | Inspections, photos, timers | proposed | No data model |
| R24 | Finance | Financial Command Center | partial | Owner view on main. PR 51 (replaces closed PR 38) open, not merged |
| R25 | Finance | 2023-2026 reconciliation | in progress | PR 51 import path. Exceptions need owner review |
| R26 | Finance | $500 daily target, job profit, investor reports | proposed | |
| R27 | Alerts | Centralized Alert Center with editable rules and delivery history | deployed | PR 64: text history, optional texts and quiet hours. Customer, urgent and backup alerts cannot be turned off. No staff alert channel yet |
| R28 | Settings | Editable Settings Center | deployed | PR 61: hours, buffer, booking services, mobile zones, labor rates, with change history. Owner saved settings Nov 8 |
| R29 | Knowledge | Knowledge Center, inbox, decisions, memory | proposed | Acceptance tests in the owner's requirement |
| R30 | Payments | Payment processing | blocked | Payments Hub trial. Waiting on rep answers |
| R31 | Divisions | Fleet, rentals, roadside | partial | Fleet customer type only |
| R32 | Divisions | NVZN Customs | partial | Model on `feature/nvzn-customs-division`, unmerged |
| R33 | Channels | Instagram, Messenger, TikTok | proposed | |
| R34 | Other | Document generation, marketing, inventory, warranties | proposed | PDF estimates exist |
| R35 | Security | Passkey login, separate accounts, approval levels, STOP ALL, automatic backups | in progress | Two-step login, STOP ALL, staff logins with role limits, nightly verified backups plus a second copy in a separate storage bucket (PR 69). Read-only off Nov 9. Both production databases have an unapplied PostgreSQL security patch (12 CVEs, notice Aug 20) that Railway's weekend window never applied; owner decision needed. No passkeys. A copy outside Railway needs an owner-made key |
| R36 | Design | One Imperial visual identity (purple and gold) across website, booking, customer pages, dashboard, staff, finance, AI, Knowledge Center, divisions and alerts; NVZN has its own division mark | approved | Owner requirement Nov 9 overrides the black and gold dashboard. Audit and tokens in `docs/DESIGN-SYSTEM.md`. Preview canvas (website home, owner home, customer approval, technician My day, shared kit) sent for approval Nov 9. No production change until approved |
| R37 | Workflow | Continuous workflow optimization across every Imperial screen (owner rule, Nov 9) | in progress | First pass: new customer to job. Next to trace: Quo conversation to estimate or appointment, owner approval from a text, technician job update, customer approval |
| R38 | Operations | Imperial as an intelligent operations manager: next-best action, durable states, exception-only alerts, recovery, failure reporting, measured savings (owner rule, Nov 9) | in progress | Built on: owner queue with statuses, owners and audit (dispatch), STOP ALL, backup failure alerts, recovery sweep, outage notices on the Customer file. First: one next step per job worked out from its records |

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
| 2026-11-08 | PR 52 is the main website version. ChatGPT's PR 59 copy is rebuilt on top of PR 52 after it merges | current |
| 2026-11-08 | PR 52 and PR 61 go live with public forms off, hidden from search, and the domain on Squarespace. PR 38 closed as replaced by PR 51. NVZN pay split decided later | current |
| 2026-11-08 | Master price sheet in `docs/PRICE-SHEET.md` is the pricing reference: $150 shop, $165 mobile, $175 diesel, $165 customer-supplied parts, $75 service call plus travel. No parts markup approved. Live website prices change only after owner verification | current |
| 2026-11-08 | ChatGPT reviews only until the owner assigns it work. Keep the QA services ChatGPT added to the preview project until testing ends | current |
| 2026-11-05 | Owner texts PAUSE ALL or STOP ALL to stop automated texting, RESUME to restart | current |
| 2026-11-09 | The whole platform uses one Imperial purple and gold design. The black and gold dashboard is not approved as final. Preview first, then production after owner approval | current |
| 2026-11-09 | Customers are created from Quo: owner reviews and ticks past contacts; new texters and callers get a record automatically | current |
| 2026-11-09 | Approval links reach customers only after the owner's YES | current |
| 2026-11-09 | Continuous workflow optimization is a permanent rule for the whole platform (R37) | current |
| 2026-11-09 | Imperial acts as an operations manager: prepare and recommend the next action, alert on exceptions, recover unfinished work, measure savings; never drop approvals for customer messages, money or deletions (R38) | current |

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
| 2026-11-06 | Private PR 52 preview in separate Railway project `imperial-pr52-preview` (website plus disposable database, commit b0555f0, access code, no production links or messaging keys) | 10 of 10 live checks passed on the preview: access code, pages, instant booking, conflict and buffer, Saturday fit, mobile, estimate, roadside, status lookup, dashboard. Production deployments unchanged | Preview only. Delete after acceptance |
| 2026-11-09 | Second copy of each nightly backup in the `offsite-backups` storage bucket (Virginia), 30 days kept, size checked, upload failure alerts the owner | PR 69, live one-off run: counts 6/6/3/49 and 441/2403 matched, offsite=2 | Yes |
| 2026-11-09 | Customer file timeline on every customer page (R08), live texts and calls through the messaging service's read-only lookups | PR 71: app-tests and messaging tests passed (one test setup fix), deployed | Yes |
| 2026-11-08 | PR 52 website and PR 61 Settings Center live, forms off, domain unchanged | Merged 18:22 and 18:24, CI passed, Railway deploys succeeded | Yes |
| 2026-11-08 | PR 62 sign-in returns to the opened page; PR 63 price questions are answered, not snoozed | CI passed, merged 19:22 | Yes |
| 2026-11-08 | PR 64 Alert Center; PR 65 My day and job assignment | CI passed, merged 19:53 and 20:12 | Yes |
| 2026-11-08 | Staff logins for Rico (tech) and the office coordinator | Created in production; first sign-in forces a password change | Yes |
| 2026-11-09 | Production read-only turned off after a verified backup | APP_READ_ONLY=false at 02:09 UTC | Yes |
| 2026-11-09 | PR 66 labor rates and estimate fixes; PR 67 per-line approvals (migration after verified backup); PR 68 Home summary | CI passed, merged 02:32, 02:44, 03:02 | Yes |
| 2026-11-09 | PR 73 approval links by text through the owner's YES; fixes a crash on a second approval round | CI passed after two fixes the tests caught, owner approved, merged 05:49 | Yes, not yet used |
| 2026-11-09 | PR 74 new-job vehicle list and phone search; PR 75 Quo import and automatic customer records | CI passed, merged 05:58 and 06:04, both services started clean | Yes, import not yet run |
| 2026-11-09 | Audit: web build for register-only PR 72 failed once at image build (transient; later deploys fine). Postgres warnings are an unapplied security patch, not data problems. Open PRs: 51, 55, 57, 58, 59 and old 4, 5, 8 | Railway environment status and Railway agent read-only check | n/a |
| 2026-11-09 | R37 optimization 1, new customer to job: before, 7 screens and about 10 taps (customer form, customer page, vehicle form, customer page, job form, job page), and a second customer could be made for a known phone. After, one form saves the customer, vehicle (VIN fills year, make and model) and job, then opens the job: 2 screens and 1 tap. A known phone opens the existing file instead of a duplicate. Adding a vehicle from the job form returns to it with the vehicle picked. Links can prefill phone and name | `tests/test_fast_intake.py`, walkthrough updated | Pending |
| 2026-11-09 | R37 optimization 2, queue text or call to job: before, open Customers, search, open the customer (or create one by retyping phone and name), New RO, pick the vehicle, retype the request: about 6 screens and 3 retyped fields. After, the queue card has Customer file and Start job; Start job carries over the phone, Quo name, vehicle (year, make, model) and what the customer said, so it is 1 tap to a filled form and 1 tap to save | `tests/test_fast_intake.py` | Pending |
| 2026-11-09 | R38 next step 1: every job page shows one Next step card (who, what, one button) worked out live from lines, approvals, the approval text, assignment, invoice and payments: add lines, ask for approval, YES the waiting text, remind the customer, assign, start, parts in, complete, invoice, collect, picked up. The job board shows the same step, replacing its separate status guesses. Before: the owner read the status, lines and invoice to decide what's next and hunted for the control (status menu, line form, invoice button). After: one button at the top. Nothing is stored, so it can't go stale; techs see only tech steps | `tests/test_next_action.py` walks a job from intake to picked up | Pending |
| 2026-11-09 | R38 exception alert 1, stuck jobs: one text a day around 11 AM (Mon–Sat), sent only when an open job's next step hasn't moved in 2+ days, listing up to 5, oldest first, with the step to take. Uses the same next step as the job page; any activity (status, line, decision, invoice, payment) takes a job off. Owner can turn it off in Alert Center; quiet hours apply. Before: a stuck job was only noticed by opening it. After: Imperial reports it, no status report on days with nothing stuck | `tests/test_next_action.py` | Pending |
