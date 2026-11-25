-- ADDITIVE ONLY: deposits a customer paid on a job before it has an invoice (the 50% deposit).
-- Existing jobs, invoices and payments are untouched. The app does NOT create tables on start and its startup check
-- lists a missing table, so apply this BEFORE deploying the code that uses it.
-- Run with owner approval, after a fresh backup. Safe to run twice.
BEGIN;
CREATE TABLE IF NOT EXISTS job_deposits (
    id SERIAL PRIMARY KEY,
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    amount DOUBLE PRECISION NOT NULL,
    method VARCHAR(32) NOT NULL,
    reference VARCHAR(128),
    received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (now() AT TIME ZONE 'utc'),
    recorded_by VARCHAR(64),
    payment_id INTEGER REFERENCES payments(id),
    removed_at TIMESTAMPTZ,
    removed_by VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_job_deposits_repair_order_id ON job_deposits (repair_order_id);
COMMIT;
