-- Apply BEFORE deploying this code. Additive and repeatable.
-- Reverse with 20261102_dispatch_contact.down.sql.
BEGIN;
ALTER TABLE dispatch_items ADD COLUMN IF NOT EXISTS contact_name VARCHAR(120);
COMMIT;
