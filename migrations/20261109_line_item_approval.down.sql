-- Reverses 20261109_line_item_approval.sql.
BEGIN;
ALTER TABLE line_items DROP COLUMN IF EXISTS decided_at;
ALTER TABLE line_items DROP COLUMN IF EXISTS approval;
COMMIT;
