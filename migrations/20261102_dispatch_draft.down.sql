-- Reverses 20261102_dispatch_draft.sql. Deletes stored drafts only.
BEGIN;
ALTER TABLE dispatch_items DROP COLUMN IF EXISTS draft_reply;
COMMIT;
