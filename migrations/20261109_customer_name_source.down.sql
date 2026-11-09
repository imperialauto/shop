-- Reverses 20261109_customer_name_source.sql.
BEGIN;
ALTER TABLE customers DROP COLUMN IF EXISTS name_checked_at;
ALTER TABLE customers DROP COLUMN IF EXISTS name_source;
COMMIT;
