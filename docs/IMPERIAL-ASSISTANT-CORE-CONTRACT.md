# Imperial Assistant: unified operational brain contract

**Owner-approved product direction (Nov 6, 2026):** Imperial is the single, natural-language brain of the business, reachable from the existing Quo owner SMS thread and the existing Imperial Operations dashboard. It should behave like an accountable office manager and project coordinator, not a narrow project-status bot, a command menu, or an additional application.

**This is the target contract, NOT a claim that all capabilities are implemented.** The read-only GitHub project update feature on this draft branch is one early adapter. Never mark the full assistant complete based on that feature.

## 1. Conversation: user intent first, tools second

The owner speaks freely: "What's going on with my shop?", "What's holding up the website?", "Have my mother handle that estimate", "Did the Fusion get scheduled?", "Add an idea for NVZN", "Look at my business finances", "Which tasks are blocked and why?". The same context should continue in the SMS thread and the dashboard.

For each turn:
1. Identify whether this is an information question, a task creation/update, a proposed action, or an approval-gated action. Resolve pronouns and follow-ups from the relevant recent conversation and durable memory, not from a single keyword.
2. Determine what exact records and source systems are needed; read the relevant authorized data from the tool registry. Consult the canonical entity record and its linked source events. Recency and provider coverage matter.
3. If necessary data is missing, try other already-connected authorized sources. If it cannot be obtained, say exactly which access/approval/field is missing and offer the next practical step; never guess, fabricate a customer, or ask the owner to repeat an accessible answer.
4. Explain concise verified findings and the next action. For permitted tasks, act, verify the resulting change, update the central task/record, and summarize the result. For approval-gated tasks, present the proposed action for confirmation.
5. Capture new decisions, project notes and unresolved dependencies into the central Knowledge & Memory Hub, with provenance, timestamps, linked project/lead/job and state. No unannounced rewriting of prior decisions.
6. Schedule and deduplicate further reminders through the persistent task engine. Continue until a verified completion criterion, an explicit customer decline, or a documented owner-authorized exceptional close.

**Plain English is the primary interface.** Older YES/NO/DONE/PROJECT commands may remain as optional shortcuts, never prerequisites.

## 2. One central source of truth, not a new app

Use the existing Imperial Operations database and UX. Canonical records:
- Customers, contact identities, vehicles, conversations/transcripts, service leads, repair orders, estimates, line-item approvals, appointments, technicians, invoices and payments.
- Projects, milestones, tasks, subtasks, owners, dependencies, due dates, approval requests, status-change history and reminders.
- Knowledge notes, past decisions, ideas, requirements, documented source pointers, verified implementation evidence and unresolved questions.
- Each record has stable IDs; source-specific identities map into it. Every inbound webhook is idempotent. Do not overwrite or close a service lead merely because a communication task was marked Done.

**Cross-channel continuity:** Quo SMS is the owner-facing interface, but the same persisted context and task state must be available on the web dashboard and to development handoffs. ChatGPT and Claude do not automatically share their private chat context or tools with a Railway-hosted assistant: preserve decisions and work items in the application's data model / GitHub / Knowledge Hub, and create authenticated integrations where supported. Do not assume connector permissions transfer from ChatGPT into Railway.

## 3. Connected capability and permission registry

Build an explicit catalog per integration: provider, available operations, authenticated owner/staff scope, connection state, last successful read/sync, last failure, record coverage, last verified update, approved writes and cost. Query actual state at runtime; don't label an integration connected solely because an env-variable name exists.

First reuse currently employed services:
- **Quo / Sona**: incoming calls, messages, transcripts, contact identity, tasks, single owner SMS thread, delivery receipts. Staff-facing messaging requires approved send policy.
- **Imperial Operations**: customer/vehicle/lead/repair order/estimate/appointment, staff assignment, inspection, signature, finance and Knowledge Hub, as implemented.
- **GitHub + Claude handoff**: repository issues/PRs, tests, branches, requirements and assigned work. Do not claim a Claude session is actively working unless a job or activity source confirms it.
- **Railway**: deployment, health, warnings, environment and preview costs when supported. No deployment from a status question.
- **Repair reference/parts**: ALLDATA and AutoZonePro (later O'Reilly) via only user-authorized, supported connector/API/browser workflows; do not imply programmatic access where none exists.
- **Calendar, Google Drive, payments/QuickBooks, fleet/rentals, Messenger**: incrementally add authenticated adapters; each must report read/write scope and known gaps. Finance is owner-only.
- **Obsidian**: optional Markdown export/mirror or offline knowledge view, not the canonical source and not a required paid subscription.

User question "Can you access X?" -> grounded capability/coverage answer with the exact missing link/credential, not a generic affirmative.

## 4. Tool-enabled action boundary

Read, propose, approve, execute, verify and log are separate states. A text response is not proof of action.

- Identity and authorization: verify owner/staff identity on the inbound Quo channel and use server-side per-tool authorization. No forwarded SMS or spoofable free-text instruction may bypass authentication.
- Low-risk internal actions (with permissions): look up records; create internal task/note; flag a blocker; stage an estimate draft; sync status. Return actual record ID.
- External/customer communications, estimate transmission, charges, cancellations, production changes, merges, domain changes, new spend, destructive actions and financial actions require appropriate explicit approval and confirmation with specific parameters. Do not let an LLM initiate tools outside the allowlist.
- Idempotent execution keys, before/after audit, provider delivery verification and rollback/compensation where possible. Report 'attempted' separately from 'confirmed'.
- STOP ALL, quiet hours, staff delegation and owner escalation must apply to autonomous outbound messages, including project notifications. No real customer texts during tests.

## 5. Persistent task engine and notifications

One universal task object rather than multiple unlinked Quo/Claude/ChatGPT to-do lists:
`task_id, title, source, project/lead/RO, created_from_event, assigned_to, priority, current_state, next_action, due_at, last_contacted_at, approval_required, evidence, verified_result, dependencies, reminders`.

Core states: new, information-needed, ready, in progress, awaiting owner approval, awaiting customer/vendor, blocked, completed-verified, explicitly declined or exceptional closure. Exactly distinguish planned vs implemented/tested/deployed/verified for software.

- Monitor customer leads on the pre-agreed 30-minute internal accountability cadence; do not text the customer every 30 minutes.
- Development/project status: a small digest and important change alerts, with dedupe. Owner can ask anything at any time; optional configurable cadence and quiet-hours.
- Assignment such as "Have Mom handle the estimates" creates a real role-scoped task only after verifying authority and assignment; send an acknowledgement after the record is actually created.
- Ask the owner only for actionable missing information or decisions; continue unrelated unblocked work independently when a dependency cannot be resolved.

## 6. Explicit acceptance test examples (fictional data)

1. "What's happening with the website?" -> verified GitHub status, automated checks, Railway preview/deployment truth, recent tester defects, blockers, next owner action, source timestamp. Follow-up "What about bookings?" retains website topic.
2. "What's happening with the dashboard?" -> unified intake/task/finance workstream status from same register, not a stale chat summary.
3. "What do I need to do today?" -> prioritized shop and development tasks, actual due dates, missing owner decisions, without duplicate tasks.
4. "What did Sona say on the last customer call?" -> authorized Quo transcript, correct customer matching, missing details, linked service lead and assigned action. If transcript unavailable, say so.
5. "Have Mom handle that standard estimate" -> verified price rules, her role, task created, and action evidence; complex estimates stay owner-gated.
6. "Text customer X their estimate" -> no send without verified item/price, matching conversation/recipient and owner-approved dispatch.
7. "Can you access O'Reilly's?" -> true connectivity and last successful read, or exact missing authorization.
8. "What did Claude change?" -> committed diff/CI evidence, not a hallucinated work-in-progress claim.
9. "Review business finances" -> only authorized owner access, source freshness/coverage and no fabricated dollar totals.
10. Replayed Quo webhook, lost GitHub connection, unknown task outcome, two customers same phone or multiple vehicles -> fail safe, seek disambiguation only if necessary; never silently complete the task.

## 7. Delivery sequence, reusing in-progress PRs

- **Foundation**: persistent lead/task engine and communication intake, issue #54 / draft PR #55. Protect data and production read-only.
- **Read-only brain**: project questions through Quo in draft PR #57, then a general source/tool router with source validity, time coverage and failure labels. Never use a single hardcoded PR list as the eventual brain.
- **Knowledge/Memory Hub**: store all business, project, customer, decision and task context in Imperial; only scoped summaries to model.
- **Safe action runner**: task assignment, follow-up and status, then audited estimates, booking, internal project commands with permission gates.
- **Consolidated daily workspace** and proactive owner SMS with clear stop rules.
- **More adapters** as justified by actual business workflows, never another overlapping operational app.

**No broad agent deployment before identity, least-privilege access, audit, test data separation and release approval are proven.**
