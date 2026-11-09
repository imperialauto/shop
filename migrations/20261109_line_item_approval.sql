-- Customer approves or declines each estimate line. Empty means not sent for approval.
BEGIN;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS approval VARCHAR(12);
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;
COMMIT;
