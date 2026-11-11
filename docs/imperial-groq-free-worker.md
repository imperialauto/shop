# Imperial — $0 incremental AI API pilot (Groq Free Plan)

**Implementation:** dependent draft PR; NOT live. **Owner intent:** execute bounded
code tasks from the private Imperial SMS queue with no paid AI API usage.

## What this pilot actually does

1. Owner sends an explicit command to the verified Quo owner thread (for example
   "Imperial, continue working on the website").
2. The existing messaging service saves the request into Imperial's
   `dev_work_jobs` database via its internal key. Customer texts NEVER enter
   this command handler.
3. Once activated, a GitHub Actions workflow polls **hourly**. Its job is
   skipped until all THREE GitHub repository variables are set to
   `true`: `IMPERIAL_FREE_WORKER_ENABLED`,
   `GROQ_FREE_PLAN_CONFIRMED` and
   `IMPERIAL_ACTIONS_ZERO_BUDGET_CONFIRMED` (owner-verified stop-at-budget policy).
4. The workflow authenticates to the public Imperial web service's
   `/internal/imperial/dev-work/worker/claim` using a **separate DEV_WORKER_KEY**.
   It never uses the more powerful internal shop/customer messaging key.
5. The server atomically claims **one website job only** with a 25-minute lease
   and audit entry. Finance, integrations and Operations development jobs stay
   saved, but are **not eligible for this narrow pilot**, and are never marked
   running or failed merely because this worker cannot handle them. Attempts
   cap at 3. Expired leases are marked **blocked for manual verification**,
   not automatically duplicated.
6. The worker uses `openai/gpt-oss-120b` via the Groq API at most **two calls
   per attempt** and at most three bounded attempts, with conservative
   prompt/response lengths. Groq rate-limit 429s
   create a recorded deferred retry, with bounded wait time.
7. It may edit only one explicitly allowlisted small public website template,
   stylesheet or public design document using an exact-match replacement.
   **Private assistant contract documents are not eligible.** Before requesting
   Groq inference, it rejects owner instructions containing identifiable
   phone/email/VIN data, dollar amounts, supplier costs or credential terms.
   After choosing a source file, it also checks that file's text for customer,
   credential or private information before sending any of its content to Groq;
   ambiguous or private source blocks the attempt. It rejects active
   HTML/script/network references in both the original selected substring and
   generated replacement.
   This is a conservative **fail-closed filter**, not a guarantee that every
   possible private fact can be detected. It never executes model-generated
   commands, accesses owner secrets, changes billing or deployment files, or
   edits customer/finance backends.
8. The runner pushes a new `agent/free-groq-<id>-<run>` branch and opens a
   DRAFT GitHub PR against `main`, then explicitly dispatches the
   pre-approved read-only Quo and shop CI workflows on its new branch.
   GitHub PRs created by the built-in GITHUB_TOKEN may otherwise require
   approval before CI starts; explicit workflow_dispatch avoids that gap.
   It records `review_needed` and its URL in Imperial, and never claims CI
   has passed merely because those runs were requested. Browser acceptance
   and owner review remain separate. No PR merge, deployment or customer messaging.
9. The dashboard and owner SMS development-status command read the same
   canonical queue. The SMS status text distinguishes *queued*, *running*,
   *blocked* and *review needed*, and never calls a configured worker an
   executing job until it actually claims the job. The owner dashboard includes
   `review_needed` PRs so reviewable work doesn't disappear. The existing
   Quo dispatcher has a separate owner-only
   notification outbox for review-needed, blocked and failed events. This
   delivery path is **OFF by default** until the owner authorizes
   `DEV_WORK_NOTICES_ENABLED=true` on both web and messaging services.
   Claims are at-most-once to avoid duplicate texts after uncertain sends.

## Prerequisites / release gates (NONE completed by this PR)

- Confirm the **Groq organization is on the Free Plan**, not its billable
  Developer plan. Do not assume a Groq API key proves free billing. In Groq's
  console, verify the actual organization's plan and limits.
- PR #148 foundation must be reviewed/merged first, followed by this worker PR
  and all combined CI. Claude's #123 touches the same owner SMS router;
  reconcile those changes and retest before deployment.
- Approval required before applying the TWO additive database migrations.
  Inspect backup/restore plan first. Rollback migrations are destructive to
  saved jobs/attempt metadata and must not run automatically.
- Railway web `DEV_COORDINATOR_CAPTURE_ENABLED=true`,
  `DEV_WORKER_ENABLED=true`, `GROQ_FREE_PLAN_CONFIRMED=true`,
  `DEV_WORKER_KEY=<random scoped secret>`, and the existing shop/web
  messaging key. Never enable before migration and audit.
- Railway messaging service owner SMS capture
  `IMPERIAL_BRAIN_ENABLED=true` and `DEV_COORDINATOR_CAPTURE_ENABLED=true`.
  Set `DEV_WORK_NOTICES_ENABLED=true` on BOTH web and messaging services
  only after successful end-to-end owner-only notice delivery tests.
  Existing `OWNER_TEXTS_ENABLED` controls whether owner texts are sent; do
  not change customer SMS settings during this work.
- GitHub Actions secrets `GROQ_API_KEY`, `DEV_WORKER_KEY`, and
  `IMPERIAL_WORK_API_URL` (the public HTTPS *Imperial web service* base URL).
  Use GitHub Actions secrets, not workflow YAML or repository files. Ensure
  the worker key is unique and scoped only to dev work.
- Before activating any GitHub Actions worker, the repository owner must
  verify its plan/remaining Actions minutes and configure GitHub billing to
  **stop usage when the $0 overage budget limit is reached**. Existing
  unrelated CI uses the same account allowance. Then set
  `IMPERIAL_ACTIONS_ZERO_BUDGET_CONFIRMED=true`.
- GitHub repository variables `GROQ_FREE_PLAN_CONFIRMED=true` and
  `IMPERIAL_FREE_WORKER_ENABLED=true` — set **last**, after all tests and
  approvals. Defaults are OFF. A test-only workflow_dispatch can be used
  after setup; every invocation must be authorized.
- In GitHub's repository Settings > Actions > General, the owner must
  review/allow workflow-created pull requests if the setting is disabled.
  The agent never approves or merges them. It uses a narrowly scoped
  `GITHUB_TOKEN` with `contents:write`, `pull-requests:write` and
  `actions:write` only during its job, to create draft branches/PRs and
  invoke TWO fixed test workflows. Branch protections and owner release
  approvals remain in force.
- Existing Railway hosting and Quo texting may still cost money.
  GitHub Actions has a finite included allowance (for example, GitHub Free
  has 2,000 monthly standard minutes); extra minutes can be billable unless
  stop-at-budget is enabled. This plan avoids *additional AI inference
  charges* within the confirmed Groq Free Plan. Never silently upgrade.

## Cost controls verified Nov 10, 2026

- Groq Free Plan API usage has no inference charge while the account is
  **actually on Free**. Rate-limited calls return HTTP 429 rather than a
  charge. Confirm this in the specific organization before enabling.
- GitHub Actions in a **private** repo consumes its plan's included monthly
  runner allowance. An hourly schedule can generate up to 720 invocations
  in a 30-day month. If each used the 10-minute maximum, that could be
  up to **7,200 runner minutes**, beyond some included allowances. This is
  a conservative upper bound, not a measured spend forecast.
- Require owner verification of GitHub's **Stop usage when budget limit is
  reached** protection and zero-dollar overage tolerance; do not substitute
  email usage alerts for a hard stop.
- The pilot uses GitHub-hosted compute (not an extra 24/7 Railway coding
  service). The existing Railway app and Quo subscriptions still carry
  their normal metered usage and fair-use limits. No new phone seat is
  intended for owner-only milestone notices.
- First controlled test should use one explicitly authorized
  `workflow_dispatch` invocation with a synthetic owner job before
  making hourly polling available; a passing CI suite is NOT a live
  cost or SMS delivery benchmark.
- Paid OpenAI/Anthropic or Groq Developer fallback is **disabled**. The
  intended additional AI inference spending cap is $0 until the owner
  separately approves a budget change.

## Limitations and next acceptance milestone

- Free Groq is rate-limited at the **organization** level. The worker reads
  HTTP 429 + Retry-After and saves a backoff; other Imperial assistants sharing
  the same Groq organization can affect available capacity.
- This is a **narrow single-file coding pilot**, not a general unrestricted
  equivalent of Claude Code, ChatGPT Work or CoWork. Broader autonomous coding
  requires a sandbox, tests, more source tools and continued security work.
- Broad instructions such as "finish everything" may not produce a safely
  bounded edit. The worker records a blocker instead of inventing work.
- Long-running work does not depend on a ChatGPT or Claude browser session.
  It depends on GitHub Actions and accessible Imperial API/DB after activation.
- The hourly schedule means the start is not instantaneous; runs may be delayed
  or skipped by GitHub. No SLA is promised.
- Quo owner-only milestone notices have isolated synthetic tests, but they
  are not active in production. Before claiming hands-off SMS updates work,
  verify an end-to-end delivery using a synthetic owner job, the authorized
  owner phone, real Quo status and the new notice outbox. Failed or uncertain
  sends must be visible and cannot be silently retried.
- Pilot acceptance: one synthetic, owner-authorized website task is captured
  once; one workflow opens an actual draft PR without merge/deploy, log
  records status; no customer/financial/private cost is exposed; CI passes;
  a 429 blocks/requeues without duplicate simultaneous run; cost stays $0
  additional API under the verified Groq Free Plan.

## Evidence

- Official Groq rate limits and 429 behavior:
  https://console.groq.com/docs/rate-limits
- Groq OpenAI-compatible API:
  https://console.groq.com/docs/openai
- Groq tool-use documentation:
  https://console.groq.com/docs/tool-use/local-tool-calling

**Owner safety stance:** no production/deployment/database change without
approval. Nothing in this document authorizes enabling paid usage.
