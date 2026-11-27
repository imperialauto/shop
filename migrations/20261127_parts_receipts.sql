-- ADDITIVE ONLY: parts store receipts (photo or PDF) kept on a job, matched by the job's PO (app/parts_receipts.py).
-- The PO number itself needs no migration: it comes from the job number (app/po_numbers.py).
-- Requires explicit owner approval before applying to any non-disposable database.
-- The app does not create tables on start: until this runs, the job page shows receipts as not switched on and
-- /readyz reports not_ready (the table is expected); every other page works. Apply BEFORE or right after deploying.
-- Safe to run twice. Reverse with 20261127_parts_receipts.down.sql (that deletes every saved receipt).
BEGIN;
CREATE TABLE IF NOT EXISTS parts_receipts (
    id SERIAL PRIMARY KEY,
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id) ON DELETE CASCADE,
    vendor VARCHAR(128),
    po_text VARCHAR(64),
    total DOUBLE PRECISION,
    note TEXT,
    matched_by VARCHAR(16) NOT NULL DEFAULT 'job_page' CHECK (matched_by IN ('job_page', 'po_typed', 'po_on_receipt')),
    filename VARCHAR(200) NOT NULL,
    content_type VARCHAR(64) NOT NULL,
    size INTEGER NOT NULL,
    data BYTEA NOT NULL,
    uploaded_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_parts_receipts_repair_order_id ON parts_receipts (repair_order_id);
COMMIT;
