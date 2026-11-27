-- Removes the tags table and every tag in it.
BEGIN;
DROP TABLE IF EXISTS tags;
COMMIT;
