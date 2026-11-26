-- Reverses 20261125_parts_eta.sql.
BEGIN;
ALTER TABLE parts_orders DROP COLUMN IF EXISTS eta;
COMMIT;
