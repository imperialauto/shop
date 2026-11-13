# Release candidate PR compatibility checks — 2026-11-13

Purpose: verify whether existing **open** PR #105 (vehicle selector staff authentication and HTML escaping) and PR #154 (remove routine oil service) can be brought onto the current `main` baseline without merge conflicts or test failures.

## Why
- Both PRs were opened against older main commits, and old CI successes do not prove compatibility with currently deployed main.
- Their fixes have real user impact: #105 addresses an unguarded staff vehicle selector and unescaped vehicle text; #154 aligns the booking catalog with the owner's no-routine-oil-service policy.
- We do **not** create competing implementation PRs. This is a disposable verification harness only.

## What the CI does
- Runs on an ephemeral GitHub-hosted runner, as two separate matrix jobs.
- Checks out latest audit PR merge base (main + this audit workflow). Fetches the *existing* PR branch and executes `git merge --no-commit --no-ff FETCH_HEAD` locally.
- Executes existing compile checks, complete Python unit tests and the public-booking PostgreSQL regression against a disposable PG 16 service.
- Never uses Railway credentials, real customers, messaging tokens, production databases or billing.
- Repository token is read-only; does not push, merge, deploy, or close existing PRs.

## Interpretation
- Green means the candidate merges into this audit base and the selected synthetic tests pass; **not** approval to merge the original PR or proof that production is safe.
- Red due to merge conflicts means a human must reconcile the original PR against current main; do not force-merge.
- Green tests are still insufficient for #105: perform adversarial authenticated/unauthenticated browser QA and owner sign-off before production.
- A future branch or deployment change invalidates this snapshot. Repeat at merge decision time.

## Gate
No production changes until reviewed, explicitly approved and final release checks pass.
