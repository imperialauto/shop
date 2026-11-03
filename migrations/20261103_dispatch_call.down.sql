-- Reverses 20261103_dispatch_call.sql.
BEGIN;
ALTER TABLE dispatch_items DROP COLUMN IF EXISTS thread_message_id;
COMMIT;
