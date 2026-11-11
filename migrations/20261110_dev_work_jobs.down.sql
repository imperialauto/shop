-- Rollback script for the *new, dedicated development-work tables only*.
-- Do not run without owner approval and a verified backup/retention plan.
-- This destroys stored development-work history if it exists.

BEGIN;
DROP TABLE IF EXISTS dev_work_events;
DROP TABLE IF EXISTS dev_work_jobs;
COMMIT;
