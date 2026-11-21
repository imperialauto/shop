-- ADDITIVE ONLY: staff can record a line approval taken in person or by phone, and the owner can waive
-- an invoice balance. Requires explicit owner approval before applying to any non-disposable database.
-- Apply BEFORE deploying the code that uses these columns (the readiness check expects them). Safe to run twice.
BEGIN;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS approval_source VARCHAR(16);
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS approval_recorded_by VARCHAR(64);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS waived_amount DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS waived_reason TEXT;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS waived_by VARCHAR(64);
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS waived_at TIMESTAMPTZ;
COMMIT;
