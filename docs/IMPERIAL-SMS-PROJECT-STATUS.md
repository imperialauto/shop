# Imperial AI — plain-English project updates (work in progress)

This adds a read-only project question handler to the **existing** owner SMS flow; it is not a new application, an additional sender, or an alternative dashboard.

## Scope
- User texts normal questions: "What's the update on the website?", "How's the dashboard coming?", "What about bookings?" after a website exchange, "Did Claude finish?".
- Imperial fetches authenticated **current GitHub pull-request metadata and latest-commit checks** for #52 website, #55 lead/workflow dashboard, and #51 financial module.
- Answers distinguish **draft, open, merged** from **production-deployed / live-verified**. GitHub merge and CI do not prove Railway deployment.
- No special commands required. Existing owner commands and customer workflows remain unchanged.
- Missing permissions, unavailable checks, or source outages produce a clear uncertainty message rather than invented completion.
- No state-changing GitHub operation, production toggle, customer text, Railway action, or project-change approval is provided by this handler.

## Deployment gate
**Feature defaults OFF**: `PROJECT_STATUS_ENABLED` must be set to `true` on the existing **shop** Railway service only after code review and tests.

A new secret `PROJECT_STATUS_GITHUB_TOKEN` is required to read the private repository. Add it **only through Railway's secret variable controls**, never commit or paste the token in GitHub/ChatGPT/Quo. Recommended GitHub fine-grained access: ONLY `imperialauto/shop`, Contents/metadata as required, Pull Requests: read, Checks: read (read access to actions if the credential is used for later event tracking). Validate scopes before enabling.

This implementation **does not** yet query Railway deployment APIs. Responses explicitly say deployment is not verified by this check. A later read-only deployment adapter, event-driven status change notifications, alert deduplication and project-completed stopping rule remain tracked in issue #56.

## Verification
Synthetic unit tests under `messaging/tests/projectStatus.test.js`. No network/production API calls during tests. GitHub PR checks must pass, and a real authenticated read must be verified in a nonproduction environment before activation. Do not assume existing GitHub connection in ChatGPT grants a token to the Railway app.

## Project references
- https://github.com/imperialauto/shop/issues/56
- https://github.com/imperialauto/shop/issues/54
- https://github.com/imperialauto/shop/pull/52
- https://github.com/imperialauto/shop/pull/51
- https://github.com/imperialauto/shop/pull/55

## Expanded prototype: unified owner tasks and reply freshness (Nov 6)

The draft PR now also includes `messaging/src/lib/imperialBrain.js` and `capabilityRegistry.js` for natural-language questions about the shop dispatch queue, GitHub issues and source access. A gated `imperial_tasks` table plus an audited internal API can capture explicit low-risk instructions such as “Add verify the website booking flow to my to-do list”. The acknowledgement must include the returned saved task ID; a failed write never produces a fabricated success message.

**Separate enablement gates:**
- `IMPERIAL_BRAIN_ENABLED=true` on `shop` enables the new source-aware owner reply router.
- `IMPERIAL_TASKS_ENABLED=true` on **both** `shop` and `web` permits internal task capture/read, but only *after* manually applying `migrations/20261106_imperial_tasks.sql`, verifying a backup/restore and completing the security release review. `web` recovery read-only mode still blocks writes. Task creation is limited to explicitly phrased internal to-dos; customer messages, delegation, billing and deployments remain gated.
- `REVALIDATE_APPROVED_REPLIES_ENABLED=true` on `shop` blocks an owner-approved customer SMS when a newer Quo message or call occurs after the draft's referenced message, or current call history cannot be verified.
- `REVALIDATE_DASHBOARD_DRAFTS_ENABLED=true` on `shop` blocks sending a stored draft from the dashboard if its Quo source message is unavailable, a newer text or call exists, or the Quo lookup fails. Manually typed messages without a draft ID are not covered by this gate and need a separately designed freshness check.

These features are **not production-enabled**. The draft's new answer routing is not an exhaustive project/task/CRM brain, and it cannot claim full real-time access to ALLDATA, AutoZonePro, Claude sessions, personal ChatGPT history, QuickBooks or Railway merely because ChatGPT has a corresponding connector. Webhook draft generation and manual Regenerate Draft now both include available recent call context, but missing transcripts and changing appointments still need explicit per-source provenance/verification.

**Regression coverage:** `messaging/tests/imperialBrain.test.js`, `messaging/tests/replyContext.test.js`, `tests/test_imperial_tasks.py`. All run with synthetic data. The next release step requires nonproduction end-to-end tests of owner identity, task persistence, call-after-draft, quoted RO change, and Quo owner notification. Keep SMS sending and task creation disabled until approved.
