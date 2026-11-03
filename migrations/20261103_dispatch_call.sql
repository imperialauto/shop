-- Apply BEFORE deploying this code. Additive and repeatable.
-- Reverse with 20261103_dispatch_call.down.sql.
BEGIN;
ALTER TABLE dispatch_items ADD COLUMN IF NOT EXISTS thread_message_id VARCHAR(128);
COMMIT;
