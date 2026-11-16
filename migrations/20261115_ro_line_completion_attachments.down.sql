-- DESTRUCTIVE ONCE USED: removes uploaded RO photos/files and line completion marks.
-- Never run automatically. Export ro_attachments first if anything was uploaded.
BEGIN;
DROP TABLE IF EXISTS ro_attachments;
ALTER TABLE line_items DROP COLUMN IF EXISTS completed_by;
ALTER TABLE line_items DROP COLUMN IF EXISTS completed_at;
COMMIT;
