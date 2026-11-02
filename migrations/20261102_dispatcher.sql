-- Apply only after backup, database audit and explicit migration approval.
-- Run after 20261102_live_overlay.sql and BEFORE deploying this code.
-- Non-destructive, additive and repeatable. Reverse with 20261102_dispatcher.down.sql.
BEGIN;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS location VARCHAR(12) NOT NULL DEFAULT 'shop';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS travel_before_minutes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS buffer_after_minutes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointment_valid_location;
ALTER TABLE appointments ADD CONSTRAINT appointment_valid_location CHECK (location IN ('shop','mobile'));
ALTER TABLE appointments DROP CONSTRAINT IF EXISTS appointment_blocked_time_nonnegative;
ALTER TABLE appointments ADD CONSTRAINT appointment_blocked_time_nonnegative CHECK (travel_before_minutes >= 0 AND buffer_after_minutes >= 0);
CREATE TABLE IF NOT EXISTS dispatch_items (
 id SERIAL PRIMARY KEY,
 quo_conversation_id VARCHAR(64) NOT NULL,
 customer_phone VARCHAR(32) NOT NULL,
 customer_id INTEGER REFERENCES customers(id),
 vehicle_id INTEGER REFERENCES vehicles(id),
 vehicle_text VARCHAR(120),
 category VARCHAR(24) NOT NULL DEFAULT 'general',
 urgency VARCHAR(12) NOT NULL DEFAULT 'normal',
 summary TEXT NOT NULL DEFAULT '',
 proposed_action TEXT NOT NULL DEFAULT '',
 approval_reasons TEXT NOT NULL DEFAULT '[]',
 status VARCHAR(16) NOT NULL DEFAULT 'routine',
 last_message_id VARCHAR(128) NOT NULL,
 decided_by INTEGER REFERENCES users(id),
 decided_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT dispatch_valid_status CHECK (status IN ('routine','needs_owner','approved','declined','closed')),
 CONSTRAINT dispatch_valid_urgency CHECK (urgency IN ('normal','urgent','safety'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_open_conversation ON dispatch_items (quo_conversation_id) WHERE status IN ('routine','needs_owner');
CREATE TABLE IF NOT EXISTS dispatch_events (
 id SERIAL PRIMARY KEY,
 quo_message_id VARCHAR(128) NOT NULL UNIQUE,
 item_id INTEGER REFERENCES dispatch_items(id),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS dispatch_policies (
 key VARCHAR(32) PRIMARY KEY,
 mode VARCHAR(12) NOT NULL DEFAULT 'approve',
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT dispatch_policy_valid_mode CHECK (mode IN ('approve','notify','auto','off'))
);
CREATE TABLE IF NOT EXISTS dispatch_audit (
 id SERIAL PRIMARY KEY,
 item_id INTEGER REFERENCES dispatch_items(id),
 actor VARCHAR(64) NOT NULL,
 action VARCHAR(32) NOT NULL,
 detail VARCHAR(255),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_dispatch_audit_item ON dispatch_audit (item_id);
CREATE TABLE IF NOT EXISTS dispatch_outbound (
 id SERIAL PRIMARY KEY,
 dedupe_key VARCHAR(160) NOT NULL UNIQUE,
 kind VARCHAR(16) NOT NULL,
 item_id INTEGER REFERENCES dispatch_items(id),
 status VARCHAR(12) NOT NULL DEFAULT 'reserved',
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT dispatch_outbound_valid_status CHECK (status IN ('reserved','sent','failed','skipped'))
);
CREATE TABLE IF NOT EXISTS dispatch_sync (
 source VARCHAR(32) PRIMARY KEY,
 last_success_at TIMESTAMPTZ,
 last_failure_at TIMESTAMPTZ,
 consecutive_failures INTEGER NOT NULL DEFAULT 0,
 last_detail VARCHAR(255)
);
COMMIT;
