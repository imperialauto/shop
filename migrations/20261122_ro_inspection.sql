-- ADDITIVE ONLY: inspection checklist rows on a repair order, shown on the customer's status page.
-- Existing jobs, lines, photos and invoices are untouched. The app also creates this table on start
-- if it is missing. Run with owner approval, after a fresh backup. Safe to run twice.
BEGIN;
CREATE TABLE IF NOT EXISTS ro_inspection_items (
    id SERIAL PRIMARY KEY,
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    area VARCHAR(80) NOT NULL,
    condition VARCHAR(12) NOT NULL CHECK (condition IN ('good', 'watch', 'now')),
    note TEXT,
    recorded_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ro_inspection_items_repair_order_id ON ro_inspection_items (repair_order_id);
COMMIT;
