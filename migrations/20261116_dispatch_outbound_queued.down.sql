-- Only after no row is 'queued' (otherwise the ADD fails and the transaction rolls back).
BEGIN;
ALTER TABLE dispatch_outbound DROP CONSTRAINT IF EXISTS dispatch_outbound_valid_status;
ALTER TABLE dispatch_outbound ADD CONSTRAINT dispatch_outbound_valid_status
  CHECK (status IN ('reserved','sent','failed','skipped'));
COMMIT;
