-- Reverses 20261106_imperial_tasks.sql (drops only the two new, empty-until-enabled task tables).
BEGIN;
DROP TABLE IF EXISTS imperial_task_audit;
DROP TABLE IF EXISTS imperial_tasks;
COMMIT;
