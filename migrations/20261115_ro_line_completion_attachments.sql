-- Recovery Nov 15 (B): per-line technician completion and RO photos/files.
-- Additive only. Existing line items, prices, approvals and invoices are untouched.
-- Run once, with owner approval, after a fresh backup.
BEGIN;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ;
ALTER TABLE line_items ADD COLUMN IF NOT EXISTS completed_by VARCHAR(64);

CREATE TABLE IF NOT EXISTS ro_attachments (
    id SERIAL PRIMARY KEY,
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    line_item_id INTEGER REFERENCES line_items(id) ON DELETE SET NULL,
    filename VARCHAR(200) NOT NULL,
    content_type VARCHAR(64) NOT NULL,
    size INTEGER NOT NULL CHECK (size > 0 AND size <= 10485760),
    data BYTEA NOT NULL,
    visibility VARCHAR(12) NOT NULL DEFAULT 'internal' CHECK (visibility IN ('internal', 'customer')),
    uploaded_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ro_attachments_repair_order_id ON ro_attachments (repair_order_id);
COMMIT;
