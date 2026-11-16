# Development worker commissioning — issue #160

Status: bounded first implementation, NOT full autonomous coding or merge-ready.
This is separate from `local_node` / Ask Imperial (#194). Do not reuse its key,
Task Scheduler entry, model prompt or database records. Intended host is the
capable home Windows desktop; confirm suitability before using a shop PC.
Windows remains Windows: Linux is only the automated test environment.

## Implemented scope

An existing development job is explicitly authorized for one host and one exact
base commit. Separate worker credentials cannot authorize work. A durable client
claim ID survives lost responses; PostgreSQL row locking and a unique running-host
index prevent two active jobs for one worker. Expired jobs never move to another
host automatically. The same host recovers its original claim while holding an OS
file lock. Never copy its state directory to another PC or run multiple state
roots with the same worker identity.

The synthetic mode creates a fixed fixture in a detached Git worktree. Restart
reuses the worktree and verifies the saved candidate rather than committing twice.
The local_patch mode makes one bounded documentation edit using localhost Ollama,
without executing model-produced commands or source code. An interrupted inference
with no saved response is blocked, not repeated. Source, model and instruction
are data, not permission to execute commands. No hosted-model billing.

A final outcome and SHA are saved on the existing development job, visible on the
owner dashboard. An identical result replay is acknowledged; a conflicting one
is refused. Completion means `review_needed`, never deployed or owner-accepted.
Artifact hashes are worker receipts, not independent security approval or CI proof.

## Prepare the PC (no activation from this PR)

1. Use a dedicated standard Windows account for the development worker. Install
   Python 3.11+, Git and an already downloaded Ollama model. Keep Ollama bound to
   127.0.0.1. Do not expose ports or register a general GitHub self-hosted runner.
2. Clone `https://github.com/imperialauto/shop.git` into a dedicated development
   checkout. Fetch the reviewed worker branch and the pinned base before testing.
   Use a clean checkout; do not point at Claude's working directory.
3. Copy `dev_worker/config.example.json` OUTSIDE the repository. Set the isolated
   QA HTTPS origin, unique workerId, absolute repository path and state directory,
   and installed local model. The state must be local NTFS, not OneDrive/network.
   Restrict configuration/state access to the worker account and administrators;
   the journal contains claim tokens. Never upload it as an artifact.
4. Store a new random 32+ character `DEV_EXECUTOR_KEY` privately in that account's
   environment. On isolated QA only, set the same `DEV_EXECUTOR_KEY` and
   `DEV_EXECUTOR_ENABLED=true`, plus `DEV_COORDINATOR_CAPTURE_ENABLED=true`.
   Keep production flags off. Do not reuse INTERNAL_API_KEY or a diagnostic key.
5. After explicit QA database approval, apply existing
   `20261110_dev_work_jobs.sql`, then `20261116_dev_executions.sql`.
   No automatic migration/startup is performed. Production needs backup and
   separate authorization. Roll back by disabling executor flag and stopping
   this scheduled task; preserve job/event/execution tables and state.
6. `py -3 dev_worker/worker.py --config D:\Imperial\Development\config.json --check`
   checks local settings only; it does not prove connectivity or commissioning.
7. Run `dev_worker/install.ps1 -Config D:\Imperial\Development\config.json`
   to register `ImperialDevelopmentWorker` for logon (does not start it).
   Use `run-worker.ps1` interactively for the first test, then test logon startup.

## Isolated acceptance procedure

Use a synthetic-only QA database without Quo, payment, real customer data or
production credentials. Through the existing owner-verified internal API, submit
a website development job with a unique synthetic message ID. Authorize it via
`POST /internal/imperial/dev-executor/jobs/{id}/authorize` using INTERNAL_API_KEY:
`{"workerId":"imperial-development-pc","baseSha":"<reviewed 40-character base>","mode":"synthetic"}`.
Worker credentials cannot call this authorization endpoint.

Start the worker with `--once` first. For interruption testing, pause/terminate the
worker process after claim or after the candidate commit but before its result is
acknowledged. Keep the same state and checkout. Restart after lease expiry. Verify:

- exactly one `claimed` event and one `final_outcome` for the same job;
- one candidate commit on the original base, one fixed fixture, clean worktree;
- expired result rejected before recovery; incorrect claim/token rejected;
- repeated identical result accepted without extra outcome; changed SHA rejected;
- owner dashboard displays the final job status and SHA;
- two processes cannot simultaneously hold the state-directory lock;
- restart after logon, offline/reconnect and model timeout show truthful outcomes.

Read the events with authenticated GET `/internal/imperial/dev-executor/jobs/{id}`.
Capture sanitized job ID, candidate SHA, worker-code SHA, Windows version,
timestamps and dashboard screenshot. Do not publish tokens or the raw journal.
A 24-hour attempt deadline fails closed and requires operator investigation.
Do not delete/re-authorize an ambiguous execution to force retries.

## Gates still required before claiming the whole #160 goal complete

- Real Windows interruption, reboot, network recovery and dashboard acceptance.
- Independent review and PostgreSQL concurrency/migration validation.
- Broader application editing in an actual sandbox; this worktree alone is NOT
  an execution sandbox and no generated application code is executed here.
- Safe draft-PR publication/reconciliation and independent CI/reviewer pipeline.
  Current candidates remain on the PC for manual review; no GitHub write token
  is installed or used by this worker.
- Owner pause/cancel controls and full canonical task/Knowledge Hub integration.

Do not call this branch ready to merge solely because synthetic tests pass.
