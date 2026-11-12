# Imperial Local Node — production rollout, rollback, and credential replacement

**Status:** plan only. Nothing here has been run against production. Every step marked
⚠ needs the owner's explicit approval at the time it runs. No secret values appear in this
document, and none should ever be pasted into chat, a PR, or a ticket.

## Production facts (verified read-only, 2026-11-11)

| Item | Value |
|---|---|
| Railway project / environment | `respectful-charm` / `production` |
| Operations app (Python) | service **`web`**, `https://web-production-94989.up.railway.app`, tracks `main` with **auto-deploy** |
| Its database | service **`Postgres`** (host `postgres.railway.internal`), with real customer data |
| Not the target | service `shop` is the **messaging** service (`/messaging`); `Postgres-Tvkp` and `db-backup-temp` are not used by `web` |
| QA (tested) | `imperial-pr52-preview / preview`, service `local-node-qa`, database `Postgres-ltXm` |

Because `web` auto-deploys `main`, **merging PR #158 deploys the code to production immediately**.
That is safe only because the feature ships switched off: without `LOCAL_NODE_ENABLED=true`
the routes return 503, the job page shows nothing new, and `/readyz` ignores the new tables.

## Rollout (in order; stop at any failed check)

1. ⚠ **Preconditions.**
   - In Railway → `web` → Variables, confirm `APP_READ_ONLY` is `false`. While it is `true`, every node write is refused with 503, and that is not a Local Node bug.
   - Confirm last night's backup job succeeded (the dashboard's backup alert must be green).
2. ⚠ **Backup.** Railway → `Postgres` (production) → Backups → create a backup. Wait until it shows complete. Note its time.
3. ⚠ **Merge PR #158** (owner). `web` redeploys `main` with the feature **off**. Check:
   - `/healthz` and `/readyz` return 200.
   - The deploy log line `imperial_database_audit` shows `"missing_tables": []`.
   - A real job page looks unchanged, with no "Shop AI diagnostic review" panel.
4. ⚠ **Migration.** Railway → `Postgres` (production) → Database → Query: paste and run the whole of `migrations/20261111_local_ai_node.sql`. It is additive and safe to run twice. Verify with:
   ```sql
   SELECT 'check', conname FROM pg_constraint WHERE conrelid = 'diagnostic_reviews'::regclass AND contype = 'c'
   UNION ALL SELECT 'index', indexname FROM pg_indexes WHERE indexname = 'uq_diag_review_one_active_per_ro'
   UNION ALL SELECT 'trigger', tgname FROM pg_trigger WHERE tgname LIKE 'trg_diagnostic%';
   ```
   Expect **8 rows**: 4 checks, 1 index and 3 triggers. Apply it **before** step 5.
5. ⚠ **Turn the feature on with a new production key, generated on the shop PC.**
   - Never reuse the QA key, and never paste the key into chat.
   - Railway → `web` → Variables: set `LOCAL_NODE_MODEL=imperial-diagnostics:latest` and `LOCAL_NODE_ENABLED=true`. Keep the Variables page open; don't deploy yet if Railway offers to stage.
   - On the shop PC, run:
     ```powershell
     powershell -NoProfile -ExecutionPolicy Bypass -File "D:\Imperial\LocalNode\install-files\install.ps1" -BackendUrl "https://web-production-94989.up.railway.app" -NewKey
     ```
   - The installer generates a 48-character key from the Windows cryptographic random source and stores it DPAPI-encrypted for this Windows user. It puts the key on the clipboard **without displaying it**, then waits.
   - In Railway → `web` → Variables, add `LOCAL_NODE_WORKER_KEY`, paste, and deploy. When the deployment is successful, press Enter in PowerShell; the installer clears the clipboard.
6. **Checks.** In the same installer run, expect every line `[PASS]`: `Railway reachable and worker key accepted` against production, `Model answers`, `Commissioning check`, and `Worker running in the background (pythonw.exe)`. Then confirm `/readyz` returns 200; it now audits the three tables. This moves the PC off QA, because one install serves one backend, so the exposed QA key is no longer stored on the PC.
7. **Production smoke check, with no customer records touched.**
   - `run-worker.ps1 -Mode eval`: synthetic cases only, nothing stored.
   - Open any job page as the owner and confirm it shows "Workstation online". Don't request a review on a real job until a technician is ready to use it.
8. **Watch the first day.**
   - `D:\Imperial\LocalNode\logs\worker.log` and `worker-startup-errors.log`.
   - Railway → `web` → HTTP logs filtered on `/internal/local-node`. Steady 200s are expected; repeated 401 means a key mismatch, and 503 means the flag is off or the app is read-only.

## Rollback (fastest and least destructive first)

1. **Stop the workstation:** `Disable-ScheduledTask -TaskName 'Imperial Local Node'; Stop-ScheduledTask -TaskName 'Imperial Local Node'`. This needs no Railway change.
2. **Switch the feature off:** set `LOCAL_NODE_ENABLED=false` on `web`, or delete it. The node API returns 503, the panel disappears and `/readyz` ignores the tables. **No data is lost.** Approved findings already in tech notes stay.
3. **Revoke the key:** delete `LOCAL_NODE_WORKER_KEY` on `web`. On the PC, run `uninstall.ps1 -RemoveKey`.
4. **Remove the code:** revert the merge commit on `main`. `web` auto-deploys the revert. Do steps 2–3 first so nothing depends on the code.
5. **Schema:** leave the three tables in place; they are inert. `migrations/20261111_local_ai_node.down.sql` **destroys the review history**. Use it only with a fresh backup and explicit owner approval. Restoring the step-2 backup is the alternative.

## Replacing the QA credentials exposed in chat

**Exposed:** these three values on `local-node-qa` only, shown in the chat on 2026-11-11: `PREVIEW_ACCESS_CODE`, `PREVIEW_OWNER_PASSWORD`, `LOCAL_NODE_WORKER_KEY`.

**Not exposed but to retire:** the `qa-office` and `qa-tech` test logins created during acceptance. Their passwords never left the build session.

**Production never used any of these values.**

Option A: keep QA running (⚠ owner does steps 1–4 personally):
1. Generate the access code and preview password on your own PC, one at a time, straight to the clipboard: `$b = New-Object byte[] 36; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b).Replace('+','A').Replace('/','B') | Set-Clipboard`. For the worker key, use `install.ps1 -NewKey`. Don't paste any of them into chat.
2. Railway → `local-node-qa` → Variables: replace `PREVIEW_ACCESS_CODE`, `PREVIEW_OWNER_PASSWORD` and `LOCAL_NODE_WORKER_KEY`. The service redeploys, the new preview password applies on restart, and the old one stops working.
3. If the PC still points at QA, re-run `install.ps1 … -BackendUrl https://local-node-qa-preview.up.railway.app`, answer **y**, and paste the new key.
4. QA app → Settings → users: deactivate `qa-office` and `qa-tech`.
5. Verify the old values are dead:
   - The old access code is refused at `/preview-access`.
   - The old owner password is refused at `/login`.
   - Railway HTTP logs show no 200s for `/internal/local-node/*` from anything but the reinstalled PC.

Option B: tear QA down (⚠ simplest once production is live):
1. Repoint the PC to production (rollout step 6). This replaces the stored key.
2. Delete the `local-node-qa` service and the `Postgres-ltXm` database in `imperial-pr52-preview`. Export first if you want the QA history. The exposed values stop existing.
3. Delete the QA scripts and installer copies under `D:\Imperial\LocalNode\install-files\` if you no longer need them; they contain no secrets.
