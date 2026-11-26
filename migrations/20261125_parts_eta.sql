-- ADDITIVE ONLY: each ordered part can carry the supplier's expected arrival day (app/parts_tracking.py).
-- Requires explicit owner approval before applying to any non-disposable database.
-- The app does not create columns on start: apply BEFORE deploying the code that uses it
-- (the readiness check expects it). Safe to run twice. Reverse with 20261125_parts_eta.down.sql.
BEGIN;
ALTER TABLE parts_orders ADD COLUMN IF NOT EXISTS eta DATE;
COMMIT;
