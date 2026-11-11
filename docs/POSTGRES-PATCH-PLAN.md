# PostgreSQL security update — preparation, risk assessment and rollback plan

**Status: PREPARE / HOLD. No owner authorization to execute database maintenance.**
**Scope:** documentation and read-only verification only. This document does not authorize an update, restart, snapshot, backup run, restore, service change, deployment, or `DATABASE_URL` edit.

## Evidence required before scheduling

| Item | Required evidence | Status |
|---|---|---|
| Database inventory | Confirm current Railway project/environment, web and messaging database service IDs, consumers, exact PostgreSQL version, image digest, storage and extension versions | NOT VERIFIED |
| Security exposure | Retrieve current Railway advisory and match affected CVEs to the *running* versions and proposed target images; assess urgency and compatibility | NOT VERIFIED |
| Backup coverage | Timestamp, success logs, offsite object identifiers, retention and integrity checks for both databases | NOT VERIFIED |
| Restore readiness | Most recent successful isolated restore for each database, schema and record-count checks, and recovery duration | NOT VERIFIED |
| Application baseline | Current deployed SHAs, health checks, connection behavior, queued work, message intake, booking and approval flow | NOT VERIFIED |
| Maintenance window | Confirm timezone, duration, on-call operator, customer impact and owner authorization | NOT APPROVED |

Historical database names and counts in earlier drafts are not reliable live baselines. Do not infer the currently running version or CVE exposure from an old advisory. Read-only checks must not reveal credentials or customer records in PR comments.

## Risks and controls

- **Downtime and reconnects:** Database restart may interrupt bookings, estimates, approvals and Quo message processing. Duration is unknown until tested; do not promise 1–3 minutes.
- **Missed or duplicate messaging:** Recovery and idempotency must be tested and monitored; do not claim Quo cannot lose messages solely because a polling job exists.
- **Schema, extensions and application compatibility:** Compare current and target PostgreSQL versions and dependencies; verify migrations and connection pool behavior.
- **Data corruption or failed startup:** Require current, verified backups and a tested independent restore route before touching either database.
- **Rollback uncertainty:** Railway snapshot availability, rollback support, restore time, data written after backup, and credential changes must be confirmed with Railway's current service capabilities. Never assume a snapshot exists.
- **Security risk of delay:** Evaluate advisory severity, exposure and compensating controls against the operational risk of the change.

## Pre-maintenance go/no-go checklist

1. Capture the live service inventory and exact running versions/digests using read-only methods.
2. Confirm the target versions address the applicable advisory without unsupported major-version jumps.
3. Verify a recent successful backup for **each** database, offsite copy and restore-test evidence. A backup success log alone is not a restore test.
4. Establish application health, record-count and queue baselines without disclosing customer information.
5. Validate the proposed change procedure and a separately tested rollback/recovery path in a nonproduction environment.
6. Define stop conditions, operator, monitoring and realistic downtime estimate.
7. Present the evidence, impact and rollback options to the owner and obtain **separate explicit approval** for a named production maintenance window.

If any item is missing or uncertain, **HOLD**.

## Proposed execution sequence — NOT AUTHORIZED

Only after separate approval: communicate the maintenance window; take and verify fresh backups; preserve the existing volumes; update **one database at a time** according to verified Railway guidance; confirm database startup, application reconnection, message intake, booking/estimate/approval operations and data integrity before proceeding to the second database. Monitor both services after completion. Never assume the messaging database has a smaller blast radius.

**Stop and escalate** on failed backups/restores, version mismatch, missing rollback capability, unexpected schema changes, sustained health failures, lost/duplicated messages, or inconsistent counts.

## Recovery decision tree — NOT AUTHORIZED

- If the database is healthy but the application cannot reconnect, diagnose connections and compare deployed configuration before considering a separately approved service restart or rollback.
- If the database is unhealthy, preserve the original data/volume and follow the **verified** Railway recovery procedure. Confirm snapshot support and compatibility before relying on it.
- If restoring a backup to a replacement service is necessary, first assess point-in-time data loss, queued writes, message reconciliation, downtime, credentials and the `DATABASE_URL` cutover. Obtain explicit authorization before any production restore or configuration change.
- Verify record integrity, latest writes, queued work and customer-facing functionality before declaring recovery complete.

## Completion evidence

Record: approval and window, pre/post versions and digests, backup/restore evidence, operator, change timestamps, health checks, message and booking reconciliation, any incident, and owner sign-off. Update R35 only after independently verifying the outcome.

**Current decision: HOLD. This PR is a planning artifact, not permission to perform maintenance.**
