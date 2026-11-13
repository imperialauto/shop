# Imperial working audit — Phase 1: infrastructure, QA baseline, public-site gates

Date: 2026-11-13. Scope: read-only GitHub/Railway verification and one synthetic-site UX fix. **This is not a complete security audit, live browser acceptance or production release authorization.**

## Verified deployment inventory (Railway connector)

- Production project `respectful-charm` (`97a71288-74a7-4405-9789-b74fda7b8939`) has `web` (FastAPI UI), `shop` (messaging, `/messaging`), two PostgreSQL services, and a backup cron service. Both `web` and `shop` reported successful deployment of commit `e29a0a0e38e3727bcf6a363db8e99c2f8cffa230` at 2026-11-12 22:54 UTC.
- QA project `imperial-pr52-preview` (`c88cae60-111e-46a5-9704-a4c2fe499c7b`) has several different services/branches and databases. `web-preview` is configured with `branch=main`, pinned commit `2dbf9f55a5aa4f732db9ec4ab89daf9ff2b91816` and last deployed 2026-11-11 23:07 UTC. **Its deployed image has not been shown to match production.** Other QA services are on separate branches; identify the authoritative QA target before owner acceptance.
- Both production database services had one Railway warning each in the 24-hour environment health overview. Warning contents and current affected versions were not established. Production database maintenance remains on hold per `docs/POSTGRES-PATCH-PLAN.md`.

## Reliability / backup evidence

- Railway 24-hour HTTP telemetry (queried on Nov 13): `web` 3,548 requests (3,479 2xx, 15 3xx, 54 4xx, **0 5xx**); `shop` 2,738 requests (all 2xx, **0 5xx**). This does **not** prove successful business-side effects or lack of dropped messages.
- Production backup cron reported `cronSucceeded` on 2026-11-12. Its logs at `20261112-1121` contain `NIGHTLY_BACKUP_OK`, `REPORTED true`, `offsite=12`, and matching live/restored **aggregate table counts** for both databases. This supports evidence of a recent scratch-restore check, **not** proof of an independently rehearsed full disaster recovery/cutover.
- Latest main-branch GitHub acceptance run on `e29a0a0` reports `158 checks, 0 failures, 2 expected 404 probes`, with synthetic data. It is not live customer/browser acceptance: https://github.com/imperialauto/shop/actions/runs/43324171007 .
- Research OSS PR #168 passed standalone LangGraph and axe-core/Playwright smoke checks and full shop regression; it does not authorize a new production dependency.

## Customer-facing issues / release gates

1. **Verified mismatch, addressed by this draft PR:** homepage promoted `Book a service`, `Request an estimate` and `Standard services you can book now` independent of `PUBLIC_FORMS_ENABLED`; service catalog prices can exist while public forms remain closed. Fix uses existing `forms_open` gate to show a working call/contact CTA, hides the instant-booking section, and adds three-state synthetic regression (closed, open, read-only). No live environment values changed.
2. **Policy backlog, do not duplicate:** main still contains routine `oil-service` in `BOOKABLE_SERVICES` even though owner requested removal. Existing PR #154 targets this. Reconcile, test and approve that PR before any launch. Existing synthetic walkthrough fixture also includes an oil-service entry; update separately once the canonical policy change is merged.
3. **Release safety:** `PUBLIC_FORMS_ENABLED` and `PUBLIC_SITE_INDEXABLE` were absent from production `web` variable names, and the source defaults both OFF. No public-domain cutover has been authorized; Squarespace remains public. Verify production startup state and owner acceptance before enabling either.
4. **SEO / content:** new site has title, description and AutoRepair structured data, but on current main the site remains deliberately `noindex`. SEO, real device experience, maps, actual search appearance, photographs, pricing and owner branding acceptance remain unverified.
5. **Autonomous execution:** #149, #163, #166 and #167 show ongoing work, but no evidence from these read-only checks that an autonomous coding worker is enabled in production. Do not equate a merged/green PR with feature activation.

## Next checkpoints (no authority to deploy)

1. Compare GitHub `main` commit with each **actual** QA deployment and run the current production-commit candidate in isolated synthetic QA. Do not infer results from the older pinned preview.
2. Reconcile #154 (oil policy), #134 (journey analytics), #149/#163/#166/#167 (development worker), #105 (RO selector auth) and other release dependencies; avoid parallel duplicate fixes.
3. Inspect current PostgreSQL warning details, exact image digest/version and patch advisory; reconcile backup retention/offsite proof and a recoverable restore plan before requesting maintenance.
4. Test customer booking, estimate, roadside, status reference, pricing, iPhone navigation, search, accessibility, PII handling and owner/staff roles on the same release candidate. No real customer sends.
5. Report security, performance and content findings with reproducible evidence; only separately approved PRs proceed to production.

## Authorization and limitations

No customer records read, no production changes, no Railway variable changes, no merges, no billing, no provider token consumption. This is an evidence record, not production acceptance. GitHub Actions on this branch must verify the public-form UX regression against test data before the fix is considered tested.
