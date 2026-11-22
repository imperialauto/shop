-- Removes the inspection checklist table and every row in it.
BEGIN;
DROP TABLE IF EXISTS ro_inspection_items;
COMMIT;
