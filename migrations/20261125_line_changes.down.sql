BEGIN;
ALTER TABLE repair_orders DROP COLUMN IF EXISTS removed_since_approval;
ALTER TABLE line_items DROP COLUMN IF EXISTS changed_at;
ALTER TABLE line_items DROP COLUMN IF EXISTS changed_from;
COMMIT;
