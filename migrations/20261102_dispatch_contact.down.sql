-- Reverses 20261102_dispatch_contact.sql. Deletes stored contact labels only.
BEGIN;
ALTER TABLE dispatch_items DROP COLUMN IF EXISTS contact_name;
COMMIT;
