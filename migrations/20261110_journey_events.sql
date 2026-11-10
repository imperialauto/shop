-- Additive migration. Apply only after owner reviews backup, risk and rollback.
-- Stores anonymous step labels only; no raw URL, entered values or IP.
CREATE TABLE IF NOT EXISTS journey_events (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(32) NOT NULL,
    page VARCHAR(24) NOT NULL,
    event VARCHAR(24) NOT NULL,
    step VARCHAR(24) NOT NULL,
    ro_id INTEGER NULL REFERENCES repair_orders(id) ON DELETE SET NULL,
    is_preview BOOLEAN NOT NULL DEFAULT FALSE,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_journey_events_time ON journey_events (occurred_at);
CREATE INDEX IF NOT EXISTS ix_journey_events_session_time ON journey_events (session_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_journey_events_ro_time ON journey_events (ro_id, occurred_at);
