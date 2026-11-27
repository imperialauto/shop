-- ADDITIVE ONLY: store invoice lines on parts receipts, and a "not matched yet" list (app/store_invoices.py).
-- Needs 20261127_parts_receipts.sql first. Requires explicit owner approval before applying to any non-disposable
-- database. Until it runs, receipts work as before (every receipt on a job, no invoice lines) and /readyz reports
-- not_ready for the two new columns. Safe to run twice. Reverse with 20261128_parts_receipt_lines.down.sql.
BEGIN;
ALTER TABLE parts_receipts ADD COLUMN IF NOT EXISTS invoice_number VARCHAR(32);
ALTER TABLE parts_receipts ADD COLUMN IF NOT EXISTS details TEXT;
-- A store invoice whose PO names no job waits here with no job until the office puts it on one.
ALTER TABLE parts_receipts ALTER COLUMN repair_order_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS ix_parts_receipts_invoice_number ON parts_receipts (invoice_number);
COMMIT;
