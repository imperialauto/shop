# Imperial Local Node — shop-workstation AI diagnostic drafts

**Status:** implemented and tested on branch `claude/local-ai-node` (draft PR). Not deployed, not merged, migration not applied anywhere.
Off everywhere until `LOCAL_NODE_ENABLED=true` **and** the additive migration is applied.

## 1. Repository audit (what was reused)

| Need | Already in `imperialauto/shop` | Decision |
|---|---|---|
| Jobs, vehicles, tech notes | `RepairOrder`, `Vehicle`, `ro.tech_notes` | Reused. Approved findings are appended to `tech_notes` |
| Job page with AI buttons | `repair_orders/detail.html` "⚡ AI Assist" card (Groq, `/ai/*`) | Reused that card; the new review sits inside it. Old Groq buttons untouched |
| Staff login, roles | `auth.py`, `permissions.py` (owner/admin/office/tech) | Reused; two regexes extended for `/ro/<id>/ai-review…` |
| Service-to-service auth | `X-Internal-Key` / `INTERNAL_API_KEY` | **Pattern** reused, key **not** reused: the node gets its own `LOCAL_NODE_WORKER_KEY` (least privilege) |
| Queue with lease/retry | `dev_work_jobs` (+ unmerged `codex/imperial-groq-free-worker-20261110`: `attempt_count`, `lease_until`, `retry_after`, scoped worker key) | Same conventions; separate tables because dev work ≠ customer jobs (per `docs/imperial-dev-coordinator.md`) |
| Append-only audit | `imperial_task_audit`, `dev_work_events` | Same pattern: `diagnostic_review_events` |
| Feature flags / unmigrated prod | `IMPERIAL_TASKS_ENABLED`, `recovery.database_audit` skip lists | Same pattern: `LOCAL_NODE_ENABLED`; `/readyz` ignores the new tables while off |
| Read-only safety mode | `APP_READ_ONLY` middleware | Unchanged; all node writes are blocked in read-only mode (no exemption added) |
| Additive SQL + down script + Postgres CI test | `migrations/*.sql`, `tests/imperial_task_postgres.py` | Same pattern |
| Alerts / task list / Jobs Home | Alert Center, My Day, Imperial tasks | **Not** duplicated. Status lives on the job page only |
| Ollama / local inference | None on any of ~150 branches | New |

## 2. Architecture

```
Tech on job page ──POST /ro/<id>/ai-review──► Railway (FastAPI + PostgreSQL)
                                              diagnostic_reviews: queued
Shop PC worker ──HTTPS, X-Local-Node-Key──► POST /internal/local-node/claim  (lease 15 min, token)
   │  (outbound only, no open ports)
   └─► Ollama 127.0.0.1:11434 /api/chat (JSON schema output)
Shop PC worker ──► POST /internal/local-node/tasks/<id>/result
                   Railway validates (app/diagnostic_guard.py) → draft_ready | retry_wait | failed
Tech ──► /ro/<id>/ai-review/<rid>: sources + draft → edit → Approve (appends to tech notes) / Reject
```

* The model receives only: vehicle year/make/model/engine/mileage, concern, tech notes, technician readings, and codes **with status exactly as entered**. No customer name, phone, email, address or VIN; phone numbers/emails inside free text are redacted.
* Code entry: `P0301 active`, `P0171 pending`, `P0420 history`. A bare `P0301` is stored as **"not supplied"** — never assumed active. This is the fix for the known P0301 issue, enforced in code, not only in the prompt.
* Draft sections are kept separate: customer-reported symptoms · reported codes · technician-confirmed findings · suspected causes · recommended tests · proposed repairs · missing information.

### Guardrails (server-side; a failing answer never becomes a draft)
Rejected and retried (max 3 attempts, then `failed`) if the answer: is not valid JSON/shape; mentions a code that wasn't supplied; changes a code's status, or calls a status-less code active/stored/pending/current; lists "confirmed findings" when the tech supplied no notes/readings; proposes any repair without a confirmed finding, or justifies one by a code alone; mentions prices, labor times, part numbers; states a numeric reading/spec not in the input; invents a model year or VIN; or claims anything was approved.
**Passing these checks is not evidence the diagnosis is correct.** The page says so.

### Approval enforcement (three layers)
1. **App code:** only `tech`, `admin`, `owner` can approve/reject; only from `draft_ready`; compare-and-swap so two people can't both decide; approved text required. Office can request and view.
2. **Database (SQLite + PostgreSQL):** CHECK constraints — approved/rejected rows must have reviewer + time; approved rows must have text; one in-flight review per job (partial unique index).
3. **PostgreSQL triggers:** new rows must start `queued` with no decision; only `draft_ready` can become approved/rejected; approval requires a validated draft; approved/rejected/cancelled rows are immutable; reviews can't be deleted; audit events are append-only.
4. The workstation has **no database credentials at all** and its key opens only the node endpoints.

Nothing is sent to customers. Approval writes to tech notes only — no line items, estimates or messages.

## 3. Files

| File | Purpose |
|---|---|
| `app/local_node_model.py` | `diagnostic_reviews`, `diagnostic_review_events`, `local_node_heartbeats` |
| `app/diagnostic_guard.py` | Code parsing, snapshot, prompt (`diag-v1-20261111`), JSON schema, validator, eval cases |
| `app/routes/local_node.py` | Worker API + staff routes |
| `app/templates/repair_orders/ai_review.html`, `_ai_review_status.html` | Review page; status fragment (polls every 5 s) |
| `app/templates/repair_orders/detail.html` | Panel inside existing AI Assist card |
| `app/main.py`, `app/permissions.py`, `app/recovery.py`, `app/routes/repair_orders.py` | Wiring (small edits) |
| `migrations/20261111_local_ai_node.sql` / `.down.sql` | Additive migration / destructive rollback |
| `local_node/worker.py` | Windows worker, Python stdlib only |
| `local_node/install.ps1`, `run-worker.ps1`, `uninstall.ps1` | PowerShell 5.1 install/launch/remove |
| `local_node/Modelfile` | Updated `imperial-diagnostics` rules |
| `tests/test_local_node.py`, `tests/test_local_node_worker.py`, `tests/local_node_postgres.py` | Tests |

## 4. Tests and results (run 2026-11-11, Python 3.11)

| Suite | Result |
|---|---|
| `tests/test_local_node.py` (guardrails, auth, authorization, approval, duplicates, offline, restart, invalid output, errors/recovery, feature-off) | **32/32 pass** |
| `tests/test_local_node_worker.py` (real `worker.py` ↔ real app over HTTP ↔ fake Ollama) | **9/9 pass** |
| `tests/local_node_postgres.py` on a disposable local PostgreSQL | **pass** (migration repeatable; every trigger/constraint refusal verified) |
| Full app flow (request → claim → lease expiry → reclaim → stale worker 409 → draft → approve; invalid ×3 → failed → retry → cancel) on PostgreSQL with triggers live | **pass** |
| Whole suite under `TZ=UTC` (as GitHub CI and Railway run) | **383/383 pass** |
| Whole suite under `TZ=America/Phoenix` | 381/383: the 2 `test_dispatch` hold-back tests fail identically on untouched `main` (see Known limitations) |

Coverage map: normal processing ✔ · missing information ✔ · unsupported repair ✔ · worker offline/restart ✔ · duplicate execution ✔ · authentication ✔ · authorization ✔ · approval enforcement ✔ · invalid AI output ✔ · processing errors & recovery ✔.
**Not tested here:** the PowerShell scripts (no PowerShell available in the build sandbox) and the real `imperial-diagnostics` model (no GPU/Ollama). Both are covered by the commissioning steps below.

## 5. QA deployment plan (NOT executed — awaiting owner review)

Railway project `imperial-pr52-preview` (c88cae60…), environment `preview` (1725c2cf…). Production project `respectful-charm` is not touched at any step.

### Which preview database
| Service | Created | Used by (evidence) | Fit for Local Node QA |
|---|---|---|---|
| **`Postgres`** (6a4470c0…) | 2026-11-06, with the project ("website plus disposable database", REQUIREMENTS-REGISTER) | `web-preview` (tracks `main`, seeds a preview owner via `preview_seed`) | **Recommended.** Runs the same Python app; nothing on it rewrites schema except `create_all` (adds missing tables only) |
| `Postgres-RPLs` (89f5f31f…) | 2026-11-08 03:46, minutes before `imperial-web-qa` / `imperial-assistant-qa` were deployed | Leads/assistant QA (`codex/imperial-qa-*` branches) | **Avoid.** `imperial-assistant-qa` runs `prisma db push` on every deploy; Prisma treats unknown tables as drift, so our tables could break that service's deploy or be dropped |

The mapping is inferred from creation times and the register; connected apps can't read `DATABASE_URL`. **Confirm before step 3:** in the Railway dashboard → `web-preview` → Variables → `DATABASE_URL`, the host should be `postgres.railway.internal` (not `postgres-rpls…`). Fully isolated alternative: add a third, new Postgres just for this test and delete it afterwards.

### Steps (each needs owner approval)
1. **Service:** add a new service `local-node-qa` in `preview` from `imperialauto/shop`, branch `claude/local-ai-node`. Generate its own Railway domain (`https://local-node-qa-preview.up.railway.app` or similar). Don't modify `web-preview`, `imperial-web-qa`, `imperial-assistant-qa` or the probe. **Don't deploy yet**: create it with no variables, or keep it stopped.
2. **Backup:** take a backup/snapshot of `Postgres` (6a4470c0…).
3. **Migration:** apply `migrations/20261111_local_ai_node.sql` to `Postgres` **before** the first `local-node-qa` deploy. `preview_seed` runs `create_all`, which would otherwise create the tables without the PostgreSQL triggers. Verify with `\dt diagnostic_*` and `\dft diagnostic_*`.
4. **Variables on `local-node-qa` only:**
   - `DATABASE_URL` = reference to `Postgres`
   - `SECRET_KEY` = new random value (not shared with web-preview)
   - `APP_READ_ONLY=false`
   - `LOCAL_NODE_ENABLED=true`
   - `LOCAL_NODE_WORKER_KEY` = new 48+ char random value
   - `LOCAL_NODE_MODEL=imperial-diagnostics:latest`
   - `PREVIEW_MODE`/`PREVIEW_ACCESS_CODE`/`PREVIEW_OWNER_PASSWORD` to match how web-preview gates access
   - No Quo, Groq, OpenPhone, `INTERNAL_API_KEY`, `OWNER_PHONE` or `PUBLIC_FORMS_ENABLED`, so this service can't message anyone.
5. **Deploy** `local-node-qa`, check `/healthz` and `/readyz` (readyz should list no missing tables).
6. **Synthetic data:** sign in, create "Synthetic Tester", 2014 Ford F-150, job with concern "Check engine light, shakes at idle".
7. **Workstation:** section 6 with `-BackendUrl https://<local-node-qa domain>`; then `-Mode eval`.
8. **Acceptance run:** P0301 with no status → draft says "status: not supplied", no repair; add tech note "Swapped coil 1 and 3, misfire moved to cylinder 3", P0301 active → repair may cite that note; offline test (stop task → "Workstation offline"; queued request completes after restart); office login can't approve; approve → tech notes + History.
9. **Teardown after QA:** remove `local-node-qa`, rotate/delete the worker key, uninstall or repoint the workstation. Tables can stay (inert) or be dropped with the down script on this disposable DB.

## 6. Workstation commissioning (Windows PowerShell 5.1)

Open **Windows PowerShell** (not as admin) on the shop PC.

```powershell
# 1. Verify Ollama, storage and GPU (paste the output back)
Invoke-RestMethod http://127.0.0.1:11434/api/version
(Invoke-RestMethod http://127.0.0.1:11434/api/tags).models | Select-Object name, size
Get-NetTCPConnection -LocalPort 11434 -State Listen | Select-Object LocalAddress, LocalPort
[Environment]::GetEnvironmentVariable("OLLAMA_MODELS","User"); [Environment]::GetEnvironmentVariable("OLLAMA_HOST","User")
Get-PSDrive D | Select-Object Used, Free
nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv
ollama run imperial-diagnostics "Code P0301, status not given. Reply in one sentence." ; ollama ps

# 2. Get the files (once the branch is pushed; pin to the commit SHA for production use)
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$src = "D:\Imperial\LocalNode\install-files"; New-Item -ItemType Directory -Force $src | Out-Null
$base = "https://raw.githubusercontent.com/imperialauto/shop/claude/local-ai-node/local_node"
foreach ($f in "install.ps1","run-worker.ps1","uninstall.ps1","worker.py","Modelfile") {
  Invoke-WebRequest -UseBasicParsing "$base/$f" -OutFile (Join-Path $src $f) }

# 3. Install (asks for the LOCAL_NODE_WORKER_KEY; input hidden). -RebuildModel backs up the
#    current Modelfile and rebuilds imperial-diagnostics with the corrected rules.
powershell -NoProfile -ExecutionPolicy Bypass -File "$src\install.ps1" -BackendUrl "https://<local-node-qa URL>" -RebuildModel

# 4. Guardrail test on the real model (synthetic cases incl. P0301 without status)
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\Imperial\LocalNode\run-worker.ps1" -Mode eval

# 5. Status / logs / restart test
Get-ScheduledTask "Imperial Local Node" | Get-ScheduledTaskInfo
Get-Content D:\Imperial\LocalNode\logs\worker.log -Tail 30
Stop-ScheduledTask "Imperial Local Node"   # job page should show "Workstation offline" within 3 min
Start-ScheduledTask "Imperial Local Node"  # queued request should then complete
```

Python 3.9+ is needed; if the installer says it is missing: `winget install -e --id Python.Python.3.12 --scope user`, open a new PowerShell window, rerun step 3.
The task starts at **logon** of this Windows user. For unattended operation after a power cut, the PC should sign in automatically or stay signed in.

## 7. Security considerations
* Outbound HTTPS only; the worker opens no port and refuses non-loopback Ollama URLs and non-HTTPS backends. Installer fails if Ollama listens on anything but 127.0.0.1/::1 or `OLLAMA_HOST` is exposed.
* Separate key (`LOCAL_NODE_WORKER_KEY`, ≥32 chars, constant-time compare). It can heartbeat/claim/report only; it can't read customers or invoices, approve, or call other `/internal` APIs. `INTERNAL_API_KEY` is not accepted on node routes and vice versa.
* On the PC the key is DPAPI-encrypted for one Windows user; the config folder ACL is limited to that user, SYSTEM and Administrators; the key exists in plaintext only in the worker process environment. Logs never contain the key or job text.
* Results require the per-claim lease token, so a crashed/duplicate worker can't overwrite another's result.
* No customer identity is sent to the model; free-text phone/email are redacted.
* **Note:** `imperialauto/shop` is a **public** repository. Nothing secret is committed here, but the prompt, guardrails and Modelfile are publicly readable.

## 8. Rollback
1. Fastest: set `LOCAL_NODE_ENABLED=false` (or remove the variable). Node endpoints return 503, the job page panel disappears, `/readyz` ignores the tables. No data loss.
2. Workstation: `powershell -File D:\Imperial\LocalNode\uninstall.ps1 -RemoveKey`, and delete/rotate the key in Railway.
3. Code: revert the merge commit.
4. Schema: `migrations/20261111_local_ai_node.down.sql` is **destructive** (drops review history). Prefer leaving the inert tables, or restoring a backup. Approved findings already copied into tech notes are not removed.

## 9. Known limitations
* PowerShell scripts were written for 5.1 but not executed in CI or the sandbox — first run on the shop PC is the test.
* The real model's quality is unverified; the `--eval` cases test guardrails, not diagnostic accuracy. Expect some drafts to be rejected and retried, especially on numeric specs (the guard rejects any number+unit not in the input).
* Text heuristics (status words, readings, labor hours) can have false positives (a correct draft gets retried) and can't catch every subtle fabrication. Technician review remains the real control.
* Single workstation, one task at a time; lease is 15 min with a 10 min model timeout.
* Worker runs at user logon, not as a Windows service.
* Approved findings go to tech notes as text; they don't create line items or estimates (by design for this phase).
* Online/offline is shown on the job page only (no separate alert, by design). A future step could add it to the existing System Health page.
* Pre-existing, unrelated: `app/routes/dispatch.py` treats naive timestamps as UTC, so `test_dispatch`'s 30 s/90 s hold-back tests fail when the machine's local zone isn't UTC. Harmless on Railway/CI (UTC) but fragile. Not changed here.
* Merge order: the open PRs #149, #134, #128, #122 and #59 already conflict with `main` in files this branch doesn't touch; this branch adds no new conflicts with any open PR (checked with `git merge-tree`).
* Starlette's `TemplateResponse(name, …)` deprecation warning is pre-existing project style; kept for consistency.
