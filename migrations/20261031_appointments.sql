-- Apply only after backup, database audit and explicit migration approval.
-- PostgreSQL production migration; non-destructive and additive.
BEGIN;
CREATE TABLE IF NOT EXISTS appointments (
 id SERIAL PRIMARY KEY,
 repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
 resource_id VARCHAR(80) NOT NULL,
 starts_at TIMESTAMPTZ NOT NULL,
 ends_at TIMESTAMPTZ NOT NULL,
 status VARCHAR(24) NOT NULL DEFAULT 'tentative',
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT appointment_positive_duration CHECK (ends_at > starts_at),
 CONSTRAINT appointment_valid_status CHECK (status IN ('tentative','confirmed','cancelled','completed'))
);
CREATE INDEX IF NOT EXISTS ix_appointments_repair_order_id ON appointments (repair_order_id);
CREATE INDEX IF NOT EXISTS ix_appointments_resource_id ON appointments (resource_id);
CREATE INDEX IF NOT EXISTS ix_appointments_resource_window ON appointments (resource_id, starts_at, ends_at) WHERE status IN ('tentative','confirmed');
COMMIT;
