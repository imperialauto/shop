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
