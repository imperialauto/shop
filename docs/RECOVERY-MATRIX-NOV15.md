# Recovery matrix, November 15 2026

Writer: Claude, branch `claude/recovery-nov15` (baseline `20360d3`). Reviewer: ChatGPT (independent, read-only).
Statuses: Planned, Implemented, Tested, Merged, Deployed, Owner-Verified. Gap classes: Regression, Unfinished, Config mismatch, Navigation, Unverified.

Evidence sources used here: Git history and code on `20360d3`; the owner's Quo thread with Imperial (+1 919-753-7167 ↔ shop line) and Antwon's Quo thread (+1 630-631-4544), both read through the Quo connector on Nov 15; Railway deployment list for `respectful-charm`. No production database rows were read by Claude; production data checks are listed for ChatGPT.

| # | Feature | Owner's expected behavior | Production (`20360d3`) | Preview / branch | Evidence | Class | Cause | Fix (this branch) | Status |
|---|---|---|---|---|---|---|---|---|---|
| A1 | Owner SMS resolves the right record | "eight for Antoine Chevy Impala" means RO #8; replies name the right customer and ID type | Free text goes to the LLM with only approval-queue items and the last 8 turns. ROs are not in context. Reply at 21:44 UTC said "For #111 with Antwon" (#111 was the Jeep Compass thread from 17:16 UTC "111 more context?") | `web-preview` (`launch-batch-nov13`) has the same `ownerAssistant.js` design | Quo AC70c1dbb…, AC19f0899…, AC4817791… | Unfinished | `ownerAssistant.answerOwner` / `interpretOwner` never resolve RO numbers or customer names; history carries the previous topic; IDs are untyped "#N" | Deterministic pre-LLM resolution of "RO 8 / job 8 / number eight / customer name" to the RO through a new internal lookup; context carries the resolved record and drops stale turns on a topic switch; replies label IDs as "RO #8", "approval #111", "task #118"; a post-check removes approval IDs the owner did not name that belong to another customer | Tested `93147ed` |
| A2 | Owner SMS app-defect capture | "I'm not able to upload photos… no options for that" is saved to the Fix list and acknowledged with its ID | Not recognized; LLM gave generic advice. `DEV_COORDINATOR_CAPTURE_ENABLED` is unset; that path needs its own table and fixed phrasing | same | Quo AC359ed48… | Unfinished + Config | No defect intent; Fix list (`/problems`, PR #187) has no SMS entry point | Defect-intent detector before the LLM; saves through a new internal endpoint to the same Fix list as "Report a problem"; reply quotes the saved ID only when the backend returns it, otherwise says it was not saved | Tested `93147ed` |
| A3 | Approval vs RO vs task IDs | IDs are never mixed up | "YES 109" after "#109 … Reply YES 109 to approve the check" sent the customer text; the owner's intent ("approve the check") was ambiguous | same | Quo AC4c74e15…, AC004b1f8…, ACe2d56fc… | Unverified | Approval prompt reused YES for a non-send step | Labels in A1; prompt wording "YES 109 sends this text" only when a text exists. Flagged for ChatGPT to confirm what #109 actually sent | Labels tested `93147ed`; #109 still Unverified |
| B1 | Per-repair completion | Mark the headlight line done while the hose and oil-leak lines stay open | Only whole-RO "Mark complete"; `line_items` has no completion columns | `web-preview` RO template has none either | `app/models.py` LineItem columns; owner SMS AC359ed48… | Unfinished | Never built (register R07 "proposed, no data model") | Additive columns `line_items.completed_at`, `completed_by`; Done/Reopen per line; authorization, technician completion, invoicing and payment stay separate states | Tested `89d28f1` (needs migration) |
| B2 | Photo/file evidence on an RO | Upload proof photos from the phone, tied to the RO and optionally a line; internal vs customer-visible | No upload anywhere in the app (only an estimate-screenshot form input) | none | grep `UploadFile`; Antwon thread shows the owner sent proof photos through Quo instead (AC4d42fb1…) | Unfinished | Never built | New table `ro_attachments` (bytes in PostgreSQL, 10 MB cap, jpeg/png/webp/heic/pdf), staff-only download, visibility internal/customer, customer-visible files on the status page only | Tested `89d28f1` (needs migration) |
| C1 | Report #122: review documents readable | Each item has Read/review showing exact wording; missing vs draft is clear; approving wording is separate from switching it on | Review list shows title + "switch it on / tick done"; no link to wording; "done" = switch on | same code in `web-preview` | `app/security_controls.py` `review_status`; template `security_center.html` | Unfinished | Review was modeled as the publish switch | Review page per item with exact current wording (or "No wording on file yet"), Approve records who, when and a hash of the wording; done = approved and wording unchanged; dashboard alert lists the pending item titles | Tested `8682b1c` |
| D1 | Outgoing texts in RO history | Every Quo text, both directions, under the right customer and RO | Outbound mirror and dispatch close only run on `message.sent/delivered` webhooks. After the owner's 19:38:30 UTC Quo text to Antwon, item #77 was re-alerted at 19:38:46 as still waiting | same | Antwon thread AC5b57f70…; owner thread ACc783e0f… | Config mismatch (likely) + Unfinished | Quo message webhook is configured by hand (only the call webhook is ensured in code); evidence indicates outbound events are not delivered. Recovery loop closes items but never mirrors messages and never passes the phone | Recovery loop mirrors both directions (idempotent by Quo message ID, original timestamps, photo marker) and passes the phone to the close call; nothing is sent. Quo webhook event list to be confirmed by the owner/ChatGPT | Tested `6eff3b5`; Quo event list Unverified |
| D2 | AI drafts for waiting customer messages | Every customer message needing the owner gets a draft or a visible failure | Health check reported 10 waiting. Drafts only come from the webhook path; recovered messages never get drafts; a failed or empty draft leaves no trace | same | `messaging/src/routes/quoWebhook.js` 209–262; `dispatchRecovery.js`; `system_health._check_drafts` | Unfinished | No retry worker and no failure state | `GET /internal/dispatch/drafts-needed`; the recovery loop drafts those (no send); failures recorded as `draft_failed` audit and shown on the card as "Draft didn't come through, retrying" | Tested `6eff3b5` |
| P1 | PR #189 My Day vs Shop Tasks | Personal to-dos separate from shop work | Not merged | `codex/my-day-personal-todos` cc4e65b | GitHub | n/a | Open PR | Review only; not duplicated here | Reviewed, see below |
| P2 | PR #190 automatic follow-ups | Follow up only when the customer went quiet after our reply; respect opt-outs, payments, completion | Not merged | `codex/automatic-customer-followups` e27f935 (stacked on #189) | GitHub | n/a | Open PR | Review only; D1 mirroring feeds its "who spoke last" signal | Reviewed, see below |
| P3 | PR #188 public navigation spacing | — | Not merged | `codex/public-navigation-spacing` 19a9f92 | GitHub | n/a | Open PR | No overlap | Reviewed, see below |

## Production checks requested from ChatGPT (read-only)
1. Quo webhook for `/webhooks/quo`: which events are subscribed (expect `message.received`, `message.delivered`, `message.sent`).
2. `communications` for RO #8: count by direction since Nov 11 vs Antwon's Quo thread (25+ messages).
3. What approval #109 actually sent at 17:16 UTC Nov 15.
4. Dispatch items in `needs_owner` with no draft: IDs and ages.

## Built on `claude/recovery-nov15` (candidate `89d28f1` until superseded)

| Commit | Gap | What changed | Tests |
|---|---|---|---|
| `8682b1c` | C1 | Read/review page per legal item with exact wording; approval records who/when/hash; edits after approval show "changed"; publish switch is separate | `tests/test_security_center.py` (25) |
| `93147ed` | A1–A3 | `app/owner_focus.py` RO resolver; `/internal/messaging/owner-focus`; `/internal/imperial/tasks/problems` (idempotent on Quo message ID); messaging saves defects before any AI and quotes the saved ID; typed IDs; guard against another customer's approval number | `tests/test_owner_focus.py` (8), `messaging/tests/ownerFocus.test.js` (7) using the owner's real Nov 15 texts |
| `6eff3b5` | D1–D2 | Recovery loop mirrors both directions with Quo IDs and times; passes phone to close; `drafts-needed` / `draft-failed`; card shows retrying / gave up | `messaging/tests/dispatchRecovery.test.js` (+4), `tests/test_dashboard_queue_v2.py` (+3) |
| `89d28f1` | B1–B2 | Line Done/Reopen; `ro_attachments`; status-page sharing; line delete follows the edit rule | `tests/test_ro_evidence.py` (10) |

Full suites at `89d28f1`: Python 492 run, 2 failures (`test_dispatch` alert-timing tests that also fail on `main`; clock-dependent); messaging 140/140.

## PR review (read-only, no code written on those branches)

Trial merge onto `89d28f1`: #188 clean. #189 conflicts in `app/routes/imperial_tasks.py`. #190 (stacked) adds conflicts in `app/routes/repair_orders.py` and `messaging/src/lib/shopClient.js`. All three are additive-text conflicts; whoever owns the merge order resolves them, one writer at a time.

**#188 public navigation spacing** (`19a9f92`): CSS and layout only (`site.css`, `site/_layout.html`). No overlap with recovery. No findings.

**#189 My Day vs shop work** (`cc4e65b`):
- My Day shows personal to-dos and a three-hour agenda; RO-linked work stays on Master Tasks. Matches the owner's ask.
- Finding R189-1 (needs a decision, not a defect): #189 says "a text does not resolve" a missed-call callback task. Production (`20360d3`, PR #187) clears every open dispatch item for a customer when the owner texts them from Quo, and D now does the same from the recovery loop. The two are different objects (task vs dispatch item), so the callback task would stay open while the dashboard item clears. Owner should confirm which he wants.
- Changes `dispatchRecovery.js` and `shopClient.js`, which D also changes; merge must keep D's mirroring and `phone` argument.

**#190 automatic follow-ups** (`e27f935`):
- Sends automatically once `AUTOMATIC_CUSTOMER_FOLLOWUPS` is "true" (unset by default). No per-message owner approval. Under the current handoff, production must keep it off until the owner approves the five message texts and the rules.
- Good: business hours and quiet hours; one text per 5 minutes; dedupe key per customer event; Postgres advisory lock; skips safety, arrival, refund and high-risk items, open appointments, jobs in progress, payments after the last contact, delivered jobs; opt-out phrases checked in the shop history and again live in Quo; re-verifies the latest Quo message before sending; never retries an ambiguous send.
- Finding R190-1 (High, fixed by D once deployed): selection reads "who spoke last" from `communications`. Without D, outbound Quo texts are missing there (D1), so a customer the owner already answered from Quo looks unanswered. The live Quo re-check limits the damage for text threads, but D should ship before or with #190.
- Finding R190-2 (Medium): the estimate rule only recognizes an outbound text containing "your estimate" plus the status link. Estimates sent with other wording never get the estimate follow-up and fall to the generic rules.
- Finding R190-3 (Low): `_rule` checks RO status `approval_received`, which is not one of the RO statuses (`intake … delivered`). Harmless, but the intended case may be unhandled.
- Finding R190-4 (Low): completed work is judged by whole-RO status only. With B, a job whose approved lines are all marked done but whose status wasn't changed still counts as active, so it is skipped (safe direction).

## Release and rollback plan (one executor; each ⚠ step needs Jaelan's go-ahead)
`web` and `shop` auto-deploy `main`, so **merging #191 is the deploy**. The migration has to be in place first: every line-item query in this code selects `completed_at` / `completed_by`.

1. ⚠ **Preconditions.** Railway → `web` → Variables: `APP_READ_ONLY` is `false`. Last night's `db-backup-temp` run logged `NIGHTLY_BACKUP_OK` (dashboard backup alert green). Candidate SHA has green CI and ChatGPT's acceptance.
2. ⚠ **Backup immediately before the migration** (house procedure, `docs/LOCAL-AI-NODE-PRODUCTION-RUNBOOK.md` step 3): run `db-backup-temp` once (dump, restore test, offsite copy) and record its `BACKUP_OK` line and table counts. **Stop** if the dump or its restore check fails. Railway snapshots aren't available on this plan.
3. ⚠ **Migration.** Railway → `Postgres` (production) → Query: run all of `migrations/20261115_ro_line_completion_attachments.sql`. Additive, inside one transaction, safe to run twice. Verify:
   ```sql
   SELECT column_name FROM information_schema.columns
    WHERE table_name = 'line_items' AND column_name IN ('completed_at', 'completed_by')
   UNION ALL SELECT 'table:' || to_regclass('ro_attachments')::text
   UNION ALL SELECT 'index:' || indexname FROM pg_indexes WHERE indexname = 'ix_ro_attachments_repair_order_id';
   ```
   Expect **4 rows**. The current production code (`20360d3`) ignores the new columns and table, so it keeps working.
4. ⚠ **Deploy:** merge #191 (web and shop redeploy together from the merge commit). Check `/healthz` and `/readyz` are 200 and the `imperial_database_audit` line shows `"missing_tables": []` and `"missing_columns": {}` (`/readyz` returns 503 if the migration was skipped).
5. **Smoke checks (no customer contact):** an RO page loads with per-line Done/Reopen and Photos & files; `/settings/security-privacy` lists Read/review links; Report a problem from `/customers/new` keeps the typed name; one owner text "RO 8 or RO 12" gets the which-job question.
6. **Rollback:** redeploy `20360d3` on `web` and `shop`. **Leave the migration in place**: the old code ignores it, and uploaded photos/files stay in `ro_attachments` for when the fix is redeployed. Run the `.down.sql` only if nothing was uploaded, or after exporting `ro_attachments` (`COPY (SELECT id, repair_order_id, line_item_id, filename, content_type, encode(data,'base64') FROM ro_attachments) TO STDOUT` into the offsite backup). Restore from step 2's dump only for data damage, not for a code problem.
7. Quo webhook: confirm `message.sent` / `message.delivered` are subscribed (owner action in Quo). D works without them through the recovery loop, a few minutes later.
