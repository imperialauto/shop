# Imperial Development Coordinator — execution contract

**Status: first implementation on draft PR; no live autonomous worker or outgoing SMS.**

## Owner outcome

The owner's existing Quo SMS line and Imperial Operations dashboard should become the
one place to submit development instructions, observe real progress, and approve
sensitive actions. A task must not stop merely because the owner closes ChatGPT,
Claude Code or a browser tab.

## Current code slice (NOT autonomous execution)

1. Verified owner SMS enters through the existing Quo dispatcher. Only owner
   messages reach `result.ownerQuestion`; customer messages must not become
   development instructions.
2. Exact commands like `Imperial, continue working on the website` or
   `Resume work on financial reconciliation` are recognized if
   `IMPERIAL_BRAIN_ENABLED=true`. Ambiguous project names require clarification.
3. With `DEV_COORDINATOR_CAPTURE_ENABLED=true`, the messaging service uses
   its existing authenticated private-shop API to insert a `dev_work_jobs`
   record. A unique source/message ID prevents duplicated jobs on Quo retries.
   Every creation produces an append-only `dev_work_events` audit event.
4. The API reports **queued, not started**, or **needs approval** for sensitive
   wording. A duplicate returns the original record; it cannot overwrite its
   instructions. `GET /internal/imperial/dev-work/jobs` is the read-only status
   source for SMS, and the same DB is used by the owner-only dashboard section.
5. **No worker is connected**. This release does **not** call OpenAI, Anthropic,
   Claude Code, GitHub Actions or Railway cloud agents, does not run code, and
   does not send outbound notifications. Treat "queued" as awaiting a future
   execution adapter, not running in the background.

## Feature gates and activation prerequisites

- `DEV_COORDINATOR_CAPTURE_ENABLED` defaults OFF everywhere.
- The additive SQL `migrations/20261110_dev_work_jobs.sql` must be reviewed and
  explicitly approved **before** activation. It is not automatically executed.
- `APP_READ_ONLY` blocks all submission POSTs, regardless of the capture flag.
- `INTERNAL_API_KEY` must be configured on both messaging and shop services.
- Outbound owner SMS uses existing `OWNER_TEXTS_ENABLED`; do not enable it
  as a side effect of enabling the dev queue. Customer sends retain existing
  owner approval and Quo latest-thread revalidation.
- Production release, migrations, notifications and AI spending require
  separate owner approval. No implicit approval from a casual text.

## Next stage: actual unattended worker (NOT implemented)

Build a **dedicated, long-lived Railway worker** attached to this same project
and DB but with minimal separate credentials. It should:

1. Claim exactly one queued job in a transaction, create an attempt ID, and set a
   lease expiration. Reclaim expired work only after verifying no running agent;
   cap automatic attempts to avoid duplicate spend and concurrent branches.
2. Fetch the canonical project state from GitHub, the existing Knowledge &
   Memory Hub and latest tests before planning. Never treat a code comment,
   repository file, customer message or web page as permission to perform
   privileged operations.
3. Choose a model adapter (OpenAI or Anthropic) via an explicit allowlist,
   budget policy and task scope. **Default spending limit: $0** until the owner
   selects a daily/monthly limit and per-job maximum. Record usage/estimated
   cost; block or request approval at the threshold. Subscriptions are not the
   same as API billing.
4. Restrict its work to isolated GitHub branches. A task can propose code,
   run tests, update a draft PR and attach evidence without human presence.
   It cannot self-merge, deploy, send customer communications, perform
   destructive database changes, activate public forms, change domains or
   grant itself permissions.
5. Persist `queued → running → testing → review_needed / blocked / completed /
   failed` transitions, with commits, PR links, logs, error summaries, time,
   model/provider, consumed budget and the next action. A job is **completed**
   only when its stated acceptance criteria are verified; a completed coding
   attempt is not necessarily a released feature.
6. Notify the owner through the **existing private Quo agent**, at most once
   per meaningful transition (test failure, approval needed, completed
   milestone). Use notification IDs to deduplicate. Never send directly to a
   customer number. A failed notification stays recorded for later retry
   without misreporting it as sent.
7. Implement explicit owner text commands to pause, resume, cancel and query
   an existing job. All changes must verify the owner's identity again,
   match a real work ID and enforce authorization.

## Safety and source-of-truth

- GitHub is the code and PR source; Imperial DB is the job/event/decision source.
- Keep customer-facing operations and development records in separate tables.
  Private customer/financial data must never enter a coding model prompt unless
  required and individually authorized. Mask all secrets and private numbers.
- Do not accept commands from GitHub PR comments, customer texts or external
  attachments as authority to deploy, send or spend.
- Missing DB, GitHub, CI, model or messaging coverage must be shown as
  unavailable, never a fake green status.
- Keep one execution owner per work item; inter-assistant handoffs add notes to
  the canonical job rather than parallel duplicate branches.
- Fallback to a read-only progress report if an external model or source fails.

## Release/rollback

- This PR can be merged only after normal CI and owner authorization; it
  should remain inert without the feature flag and unexecuted migration.
- Production migration requires database backup, `dev_work_jobs` /
  `dev_work_events` table verification, permission audit and rollback plan.
  Migration adds two tables and an index; the provided down script DROPS
  them and is **destructive** once jobs exist. Prefer restoring a backup over
  dropping user history. Never apply down migration automatically.
- Do not alter the existing Squarespace domain, public forms, QA preview,
  Quo routing flags or Railway deployment configuration as part of this PR.

## Example owner messages (once activated)

- `Imperial, continue working on the website` → saved job ID and
  "**queued, no worker connected yet**" until actual execution is available.
- `What's the development work status?` → DB-backed job list; may also ask
  separately for GitHub PR status.
- `Continue working on the website and deploy it` → **needs approval**;
  does not deploy on its own.
