# Imperial: OSS candidate verification, not adoption

This branch contains **isolated, synthetic** upstream-library smoke tests. No Imperial app imports, DB migrations, provider credentials, real customer records, production environment variables, existing settings or dashboard routes are changed.

## Candidate checks

1. **LangGraph** (MIT; langchain-ai/langgraph). The pinned Python package must import on the existing Python runtime, compile a small workflow, and distinguish unapproved/approved synthetic tasks. This is **not** a production integration, evidence of durable persistence, or authority to invoke a coding model. Reconcile with the existing dev work lease/attempt model and draft PRs #149/#163/#167 before further work.
2. **axe-core + Playwright** (MPL-2.0 / Apache-2.0; dequelabs/axe-core and microsoft/playwright). Launch headless Chromium; confirm a synthetic valid page passes and an intentionally inaccessible page produces an image-alt violation. This is **not** an audit of the customer website. Integrate into the existing synthetic QA walkthrough as a separate reviewed PR after smoke success and false-positive assessment.

Only GitHub-hosted disposable runners with read-only repository permissions; tests have no secrets or access to Railway, Quo, Imperial DB, live customers, or model APIs. Dependency packages still execute in their own isolated CI container; inspect the supply chain before any privileged deployment.

## Adoption gates

- Passing this smoke proves package/API operation **only**. It does not prove fitness, security, cost, runtime stability, DB/role isolation, customer accessibility, or end-to-end operation in Imperial.
- A separate draft integration PR must pin dependencies, add negative/permission/failure/recovery tests, pass full app CI, run in synthetic QA, and be owner-approved before any production release.
- Existing OpenCode home PC installation requires separate observed CLI/worker testing. Do not treat this branch as evidence that OpenCode can autonomously implement tasks.
- Do not install LiteLLM as part of this pilot; investigate the March 2026 supply-chain incident and pin/audit any future provider gateway.
- No default automatic merge/deployment.

Reviewed candidate releases on 2026-11-13:
- https://github.com/langchain-ai/langgraph/releases/tag/1.2.21
- https://github.com/dequelabs/axe-core/releases/tag/v4.14.0
- https://github.com/microsoft/playwright/releases/tag/v1.65.0
