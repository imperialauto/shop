-- Reverses 20261102_dispatcher.sql. Deletes the queue, audit and sync history
-- and the appointment travel/buffer values. Take a backup first.
BEGIN;
DROP TABLE IF EXISTS dispatch_sync;
DROP TABLE IF EXISTS dispatch_outbound;
DROP TABLE IF EXISTS dispatch_audit;
DROP TABLE IF EXISTS dispatch_policies;
DROP TABLE IF EXISTS dispatch_events;
DROP TABLE IF EXISTS dispatch_items;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointment_blocked_time_nonnegative;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointment_valid_location;
ALTER TABLE appointments DROP COLUMN IF EXISTS buffer_after_minutes;
ALTER TABLE appointments DROP COLUMN IF EXISTS travel_before_minutes;
ALTER TABLE appointments DROP COLUMN IF EXISTS location;
COMMIT;
