# Imperial Development Center: voice, autonomous work, local coding, recommendations

**Status:** proposal only; documentation PR for [issue #160](https://github.com/imperialauto/shop/issues/160). No implementation or deployment is implied by this file.

## User outcome

The owner can dictate or type an idea in the **existing** Imperial Operations dashboard, get a grounded assistant reply and permanent task ID, leave, and return to a visual history showing actual work, PRs, QA previews, test evidence, suggestions, failures, and approvals. An independent worker continues through authorized queued tasks while the owner is away.

This is not permission to self-merge, self-deploy, enable customer messages, activate public forms or migrate production data.

## Verified repository baseline (Nov 12, 2026)

| Area | Repository evidence | Gap |
|---|---|---|
| Development command intake | Merged #148; `app/routes/dev_work.py`, `docs/imperial-dev-coordinator.md` | Main's queue supports capture/status but explicitly has **no coding executor**, and feature activation/migration not independently verified |
| Draft worker | Open draft PR #149, `docs/imperial-groq-free-worker.md` | Narrow one-file worker; disabled; prerequisites and cost/CI/notice QA outstanding |
| Local AI | Merged #158, `docs/LOCAL-AI-NODE.md` | Diagnostic-draft local node; not general coding; runtime readiness separate from code merge |
| Canonical tasks | Issue #151 | Reconcile/synchronize task records, sources, status transitions, Knowledge & Memory Hub |
| Existing owner messages | Issue #56, Quo paths | One verified owner-only notification channel, milestone updates with dedupe |
| Website experience | Issues #95/#109; PR #156 merged; PR #134 tracking staged | Revalidate mobile booking, reference recovery, preview acceptance, navigation; customer analytics gated |

**No assumption:** A merged PR is not equivalent to deployed, feature-enabled or owner-accepted. Before new code, inspect deployed commit, flags, migration tables, QA environment, existing `My Day` UI and the latest open PRs.

## Proposed design

### One dashboard, existing data

- Add an **owner-only Development** view to the current application, not a second app/task system.
- Sections: **Conversation**, **Ideas & recommendations**, **Work queue**, **Agents**, **Previews & test evidence**, **Approvals**, **Usage & errors**. Use responsive existing design tokens.
- Task card records canonical ID, project/requirement, spec revision, owner, source, priority, dependencies, current state, next action, worker lease, timestamps, branch, PR, CI status, QA environment, cost, blockers and audit trail.
- Two-way conversational UX: user can say "finish the mobile booking flow"; system acknowledges **captured** with task ID, may summarize known state, and never states **started** or **completed** before backend evidence. Conversations and attachments link to the same canonical records.
- Avoid duplicate Jobs Home, Alerts or My Day cards; all must be views over #151 canonical tasks.

### Voice input/reply MVP

1. Accessible mobile text input that supports operating-system keyboard dictation (iOS and Windows), with clear send/review control.
2. Persisted chat reply and event timeline; optional user-triggered `speechSynthesis` playback (check Safari accessibility).
3. Later: push-to-talk audio recording and server-side transcription, or local transcription when home node is online. Audio upload must be permissioned, time-limited, size-limited, encryption-protected, with configurable retention and explicit deletion.
4. Optional realtime conversation only after cost/privacy review; browser obtains short-lived token from trusted backend, never direct provider key. Preserve input transcript plus task action confirmation.
5. Test iOS Safari/PWA, Windows, microphone denial, canceled recording, disconnection/retry, duplicate submissions, role isolation and no commands inferred from customer voice.

### Autonomous coordinator + coding worker

- Persistence lives in Imperial DB; code and tests live in GitHub. Event-driven enqueue/worker claims and a scheduled reconciliation job prevent missed webhooks. Do not rely solely on GitHub cron: it can run late or drop under load.
- States: `proposed -> approved_for_development -> queued -> claimed -> implementing -> testing -> review_needed -> accepted_for_release -> deployed -> owner_verified`; plus `blocked / paused / failed / canceled`. Record every transition and evidence. Do not label work finished based only on a merge.
- Job lease + worker heartbeat + isolated worktree/branch + idempotency keys + bounded attempts + deterministic retries/backoff; on crash, verify no worker is still active before reassigning. Only one active writer per branch/task.
- Agent-specific permissions: planner reads sanitized requirements, developer writes isolated branch, test runner executes sandboxed allowlisted checks, reviewer reports evidence; release approver must be owner.
- Batch independent, approved tasks; safely continue others if one is blocked. Do not let agents self-expand scopes, grant permissions, or follow instructions from untrusted website/PR/customer text.
- Produce draft PR, CI run links, test artifacts, synthetic QA preview, screenshots and a concise readable summary. No automatic production deployments/customer sends.

### Hybrid local/offline executor

- Home Windows PC is intended development machine; shop PC is **not** a suitable coding host.
- Use Ollama + OpenCode on home PC for feasible local coding work. `opencode run` supports headless execution, but choose a model only after measuring RAM, GPU VRAM, context capacity, latency, and quality on representative synthetic Imperial tasks.
- DeepSeek local/distilled models can be evaluated alongside local coding-oriented models; DeepSeek hosted is optional and only with appropriately sanitized source plus explicit cost/data handling approval. Claude Code is for separately authorized advanced tasks/review.
- Avoid registering the personal desktop as a general GitHub Actions self-hosted runner while the repository is public (untrusted PR execution risk). Prefer a **separate scoped outbound polling worker** to fetch authorized jobs over HTTPS, sandboxed git worktrees and allowlisted test commands; no inbound public port/Ollama exposure. Never mix customer diagnostic node credentials with development worker keys.
- Offline means no hosted token API while running a cached local model. The PC still needs power/appropriate hardware; GitHub, dashboard, Railway and preview sync still need network. Queue persists when the PC is unavailable.
- Default to **no new paid API spending** until owner sets per-task/daily/monthly budgets and approves provider use. Track cloud runner/hosting cost and all model usage.

### Proactive evidence-based improvements

Signal sources: existing QA test failures, booking/estimate/status funnel, support and Quo problems, performance/accessibility checks, customer journey events **only if PR #134 is approved and feature activated**, regressions and duplicate prompts/fields.

Propose a card with problem, reproducible evidence, customer impact, effort estimate, lowest-risk test, expected benefit and Accept/Defer/Dismiss. Recommendations must be deduplicated against #151 and issue/PR ledger. Do not invent conversion or revenue metrics, and do not auto-release a proposal.

Initial candidates grounded in existing backlog:
- Mobile booking step progression and form collapse (PR #156 was merged; verify release/QA acceptance before opening redundant fix).
- Booking reference retention/recovery and privacy-safe status lookup (issue #95).
- Self-service estimate intake without defaulting to phone calls, including pricing and approval gates (issue #109).
- Instrumented funnel analysis after privacy review, migration and explicit activation (PR #134).
- Visual mobile regression screenshots and accessibility checks on each website PR.

### Security, operation and release

- Repository is public. Audit for secrets/private customer data and keep sensitive logs/financial records/Quo threads out of it and out of unapproved hosted model prompts.
- Authenticate owner UI and action API; separate staff roles, development worker keys, customer diagnostic worker, and owner notification key; rotate keys when changing environments.
- Protect against prompt injection in external input. Tool capabilities and approval decisions are enforced server-side, not merely in a prompt.
- Write append-only task/notification audit, avoid duplicate sends and speculative progress states; missing integrations show `unavailable`, not a fabricated success.
- Backup and rollback review **before** any production DB migration. Down scripts which drop work history are destructive; preserve records rather than silently deleting.
- Production merges, activation flags, provider billing, public forms/domain cutover, real customer SMS and destructive changes remain owner-approved.
- Maintain Squarespace public site until separate launch acceptance; use isolated synthetic QA for tests.

## Sequence of PR-sized deliverables

0. **Baseline audit:** compare main and open branches, current production/preview deployments, flags, DB migrations and CI. Provide dependency map, budget plan, threat model, rollback and gaps; no production writes.
1. **Unified task/idea ingestion** (integrate #151): owner text idea -> canonical task + conversation + dedupe + status/history.
2. **Owner chat and voice MVP**: accessible conversation, native dictation text input, persisted grounded responses, optional spoken playback; test on actual iPhone/Windows browsers.
3. **Bounded coding executor**: reconcile #149 with current main, safe job lease, draft PR + CI verification using synthetic website-only task; verify browser may close and work continues on hosted worker.
4. **Home node pilot**: isolated authenticated outbound local worker, OpenCode/Ollama benchmark, online/offline recovery, branch/test evidence; no direct desktop exposure.
5. **Recommendations and preview acceptance**: evidence-backed suggestion cards and QA screenshots with opt-in triage.
6. **Release review**: backend health, backups, migration permissions, cost controls, QA evidence and owner signoff before any live changes.

## End-to-end acceptance scenario

1. On iPhone, owner dictates "Make mobile booking less confusing." Transcript can be reviewed and edited.
2. Owner submits once; system returns task ID and knows current #156/#109/#95 state, without duplicating.
3. Approved low-risk development task is claimed once and runs in an isolated branch after browser closes.
4. Worker records actual commit, tests, CI results, and draft PR; if unavailable, state is visibly blocked with reason, and another eligible task can run.
5. Owner receives dashboard reply and optionally deduped Quo milestone notice, previews the change and says "Move that button up." Feedback becomes a revision of the same task.
6. Recommendation engine proposes a related enhancement with evidence and a triage control, without silently deploying.
7. No customer data/sends, production changes, unapproved billing, or false claims occur.

## References

- [Issue #160 — user outcome and acceptance](https://github.com/imperialauto/shop/issues/160)
- [Issue #151 — canonical task queue/Knowledge Hub](https://github.com/imperialauto/shop/issues/151)
- [PR #149 — draft constrained worker](https://github.com/imperialauto/shop/pull/149)
- [MDN — SpeechRecognition compatibility](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition)
- [OpenAI — voice agent and WebRTC guidance](https://developers.openai.com/api/docs/guides/realtime)
- [Ollama — OpenCode integration](https://docs.ollama.com/integrations/opencode)
- [OpenCode — CLI noninteractive run](https://opencode.ai/docs/cli)
- [GitHub — public repository self-hosted runner risk](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)
