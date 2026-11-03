-- Apply after 20261102_dispatcher.sql and BEFORE deploying this code.
-- Additive and repeatable. Reverse with 20261102_dispatch_draft.down.sql.
BEGIN;
ALTER TABLE dispatch_items ADD COLUMN IF NOT EXISTS draft_reply TEXT;
COMMIT;
