-- DESTRUCTIVE to worker attempt metadata. Requires explicit owner approval.
-- Do not apply during an active agent run or without backup.
BEGIN;
DROP TABLE IF EXISTS dev_work_notices;
ALTER TABLE dev_work_jobs DROP COLUMN IF EXISTS retry_after;
ALTER TABLE dev_work_jobs DROP COLUMN IF EXISTS lease_until;
ALTER TABLE dev_work_jobs DROP COLUMN IF EXISTS attempt_count;
COMMIT;
