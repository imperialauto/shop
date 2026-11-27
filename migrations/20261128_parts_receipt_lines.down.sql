-- Reverses 20261128_parts_receipt_lines.sql. Deletes receipts still waiting without a job, and the invoice lines
-- read off every receipt (the receipt files themselves stay on their jobs).
BEGIN;
DELETE FROM parts_receipts WHERE repair_order_id IS NULL;
ALTER TABLE parts_receipts ALTER COLUMN repair_order_id SET NOT NULL;
DROP INDEX IF EXISTS ix_parts_receipts_invoice_number;
ALTER TABLE parts_receipts DROP COLUMN IF EXISTS details;
ALTER TABLE parts_receipts DROP COLUMN IF EXISTS invoice_number;
COMMIT;
