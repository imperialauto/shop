-- Apply only after backup, database audit and explicit migration approval.
-- Run after 20261031_appointments.sql. Non-destructive, additive and repeatable.
BEGIN;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS booked_value NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (booked_value >= 0);
CREATE TABLE IF NOT EXISTS booking_slots (
 id SERIAL PRIMARY KEY,
 resource_id VARCHAR(80) NOT NULL,
 starts_at TIMESTAMPTZ NOT NULL,
 ends_at TIMESTAMPTZ NOT NULL,
 removed_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT booking_slot_positive_duration CHECK (ends_at > starts_at)
);
CREATE INDEX IF NOT EXISTS ix_booking_slots_active ON booking_slots (starts_at) WHERE removed_at IS NULL;
COMMIT;
