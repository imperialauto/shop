-- ADDITIVE ONLY: estimate lines can be changed after the customer saw or approved them, and the
-- customer's approval page shows what changed (app/line_changes.py).
-- Requires explicit owner approval before applying to any non-disposable database.
-- The app does not create columns on start: apply BEFORE deploying the code that uses them
-- (the readiness check expects them). Safe to run twice.
BEGIN;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS changed_from TEXT;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS changed_at TIMESTAMPTZ;
ALTER TABLE repair_orders ADD COLUMN IF NOT EXISTS removed_since_approval TEXT;
COMMIT;
