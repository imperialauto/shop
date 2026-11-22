-- Dashboard replies wait in dispatch_outbound as 'queued' until the messaging service
-- picks them up (app/routes/dispatch.py queued_replies). The original constraint
-- (20261102_dispatcher.sql) never allowed 'queued', so on PostgreSQL every approved
-- dashboard reply since Nov 5 failed its insert and the decision was rolled back.
-- Additive: widens the allowed values; existing rows are unaffected. Safe to run twice.
BEGIN;
ALTER TABLE dispatch_outbound DROP CONSTRAINT IF EXISTS dispatch_outbound_valid_status;
ALTER TABLE dispatch_outbound ADD CONSTRAINT dispatch_outbound_valid_status
  CHECK (status IN ('queued','reserved','sent','failed','skipped'));
COMMIT;
