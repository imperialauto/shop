# PostgreSQL security update: risk assessment and rollback plan

**What:** apply Railway's pending PostgreSQL vulnerability update (notice Aug 20, 12 CVEs) to both production databases in `respectful-charm`. The weekend maintenance window never applied it.
**Owner approval:** Nov 9, "I've already said yes to this entire list" (Postgres security update tonight). This plan was written per the Nov 9 continuation rule: no database maintenance without a documented risk assessment and rollback plan.
**When:** Nov 9, 23:05 Arizona (06:05 UTC Nov 10), after closing and outside the 11:17 UTC nightly backup.

| Database | Service | Used by | Data |
|---|---|---|---|
| Postgres-Tvkp | e252377e | messaging (`shop`) | conversations, drafts, delivery state |
| Postgres | eebd83f8 | web (Operations) | 444 customers, jobs, invoices, queue, audit |

## Risks
| Risk | Likelihood | Effect | Control |
|---|---|---|---|
| Update restarts the database | Certain | 1–3 min outage; app reconnects | Off-hours; messaging's 5-minute recovery re-reads Quo afterwards, so no texts are lost |
| Image/version change breaks startup or data files | Low | Database down | Verified backup taken first (restore-tested); Railway volume snapshot; restore steps below |
| App can't reconnect (stale pool) | Low | 500s until restart | Restart the web and shop services; check `/healthz` and the startup audit |
| Data loss | Very low | Customer/job records | Fresh dumps of both databases, restore-tested, copied to the offsite bucket before any change |
| Owner texts or alerts sent twice after restart | Low | Duplicate owner alert | Outbound rows are reserved before sending; recovery is idempotent (existing tests) |

## Before (stop if any step fails)
1. Run the backup job once: dumps both databases, restore-tests each into a scratch database, uploads to `offsite-backups`. Proceed only on `NIGHTLY_BACKUP_OK` and both offsite uploads confirmed.
2. Record counts from the web startup audit: customers, repair_orders, dispatch_items, users. Baseline Nov 9: 444 / 6 / 49+ / 3.

## Steps
1. Postgres-Tvkp (messaging) first, because it has the smaller blast radius. Apply the update and wait for healthy.
2. Check: the shop service logs show no connection errors, and the next `/internal/dispatch/intake` and `/sync` calls return 200.
3. Postgres (web). Apply and wait for healthy.
4. Check: restart web if needed; the startup audit shows `missing_tables: []`, `missing_columns: {}` and counts equal to or above the baseline; `/healthz` returns 200.
5. Put `db-backup-temp` back on its nightly schedule.

## Rollback
- **App errors but the database is healthy:** redeploy the last good web or shop deployment (Railway rollback). No data change.
- **Database won't start, or data is wrong:** restore that service's volume from the Railway snapshot taken by the update. If that's not possible, create a new Postgres service and restore tonight's verified dump into it, then point only that app's `DATABASE_URL` at it. This changes production settings, so it's done only with the owner told immediately. Never drop or overwrite the original volume.
- **Stop condition:** any failed check in "Before" means the update isn't applied tonight, and the owner is told why.

## After
- Update register R35 with the result, the counts before and after, and the backup file names.
- Tell the owner in plain words.
