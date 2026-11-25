-- Rollback for 20261124_job_deposits.sql. Only after the code that uses it is rolled back.
-- Deposits already carried onto invoices stay as payments there. Unapplied deposits are lost, so export them first:
--   SELECT * FROM job_deposits WHERE payment_id IS NULL AND removed_at IS NULL;
BEGIN;
DROP TABLE IF EXISTS job_deposits;
COMMIT;
