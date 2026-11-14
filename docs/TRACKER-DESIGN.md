# Imperial Tracker — owner's mobile task tracker (design, Nov 14, 2026)

Status: design reviewed with the owner, not built. Post-launch work. Mockup: Claude design canvas "Imperial Tracker — mobile" (9 phone screens), https://claude.ai/artifact/CgkaEgvseCF4pj8NdN8aFD (owner's private link).

## Owner requirements (his words, condensed)
- Stay in the Imperial app all day: calls, texts and email in one place.
- No long scrolling pages on mobile; everything findable in one screen.
- Every task can be checked off and moves to Done; or Waiting, Tomorrow, Pick a time. If none of the options fit, he can type what he wants instead of abandoning the item.
- Jot box: any note is saved first, then becomes a task, reminder or idea.
- Replies must sound human and like him. A "Rethink" option when a draft doesn't make sense.
- Follow-ups the system catches on its own: customer gone quiet → draft a follow-up; missed call → text; caller hangs up on the AI → text "what can I help with?".
- Schedule and automate tasks at set times.
- Invite people to a task, including people outside the shop (e.g. a parts runner) without an account.
- Follow-up stats.
- Tracker replaces the booking calendar as the first mobile tab.

## Screens
1. Tracker home: jot box, tabs Now / Waiting / Later / Done with counts, one "caught overnight" banner, compact rows with check-off. Done tab shows weekly stats.
2. Task actions sheet: Done, Waiting on them, Tomorrow, Pick a time, Call instead, Hand it off, plus "None of these fit? Tell Imperial".
3. Reply: draft in the owner's voice (learned from his sent texts), Rethink (Shorter, Warmer, Add our hours, Start over, "what's off?"), Send / Send at….
4. Inbox: calls, texts and email together, each with its follow-up state.
5. Task detail: next step on top; steps, blocked-by, linked records (job, thread, estimate, PR), people, history folded.
6. Send a task: Parts run / Help on job / Take it over; filled from the job; helper sees only that task; no customer contact info; never part costs; link expires when done or in 24 h.
7. Helper's link page: part, supplier with directions, order # and paid status, where to bring it; buttons Picked up / Receipt photo / Problem / Call the shop.
8. Follow-ups: You promised / No reply yet / Gone quiet / Hung up on AI, each with a draft ready.
9. Automations: missed call, hang-up on AI, gone quiet 2 days, estimate unapproved 24 h, promised update, 10 AM daily briefing, one alert per item, app work for AI agents needs owner OK. Each: Sends on its own / Drafts for me / Reminds me / Off.

## What already exists (Nov 14 audit of main f8afc4e)
- Missed calls and voicemails are detected (`app/routes/dispatch.py`). A Sona-answered call where the caller hangs up is labeled `customer_reply`, not missed → no follow-up today. Gap.
- Snooze: texting agent supports `LATER n` (hours/minutes, max 7 days) (`app/dispatch.py`).
- Owner tasks captured by text with `due_at` (`app/imperial_task_model.py`, `app/routes/imperial_tasks.py`, behind `IMPERIAL_TASKS_ENABLED`).
- One task per job on Home; job `next_action` is the single source of the next step.
- No-login customer links (`/status/<token>`) — same pattern for helper links.
- No email integration in the app.
- Agent alerts repeat for the same item (Hellcat #102 alerted 6 times on Nov 13) — needs one-alert-per-item.

## Build order (agreed, with ChatGPT's Nov 14 handoff as a gap check)
1. Reconcile every existing task source (dispatch items, imperial tasks, service leads, job next steps, dev work, the Nov 11 workbook) and report duplicates before building.
2. One canonical task record with links to existing records (they keep their own ids), append-only history, idempotent sync.
3. These screens on top, as views of that record (My Day, Jobs, Alerts stay consistent).
4. Automations and notification dedupe; then email inbox; then AI-agent work coordination.
Safeguards unchanged: no customer messages, merges to production, migrations or domain switch without the owner's approval; synthetic data for tests.
