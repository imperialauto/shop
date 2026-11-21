BEGIN;
ALTER TABLE invoices DROP COLUMN IF EXISTS waived_at;
ALTER TABLE invoices DROP COLUMN IF EXISTS waived_by;
ALTER TABLE invoices DROP COLUMN IF EXISTS waived_reason;
ALTER TABLE invoices DROP COLUMN IF EXISTS waived_amount;
ALTER TABLE line_items DROP COLUMN IF EXISTS approval_recorded_by;
ALTER TABLE line_items DROP COLUMN IF EXISTS approval_source;
COMMIT;
