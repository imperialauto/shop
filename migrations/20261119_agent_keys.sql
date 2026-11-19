-- ADDITIVE ONLY: assistant (Claude) access keys and their change log.
-- Requires explicit owner approval before applying to any non-disposable database.
-- Creates two new tables. Does not alter or read any existing table's data. Safe to run twice.
-- After applying, set AGENT_API_ENABLED=true on the web service to turn the feature on.

BEGIN;

CREATE TABLE IF NOT EXISTS agent_keys (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    key_prefix VARCHAR(16) NOT NULL,
    key_hash VARCHAR(64) NOT NULL UNIQUE,
    scopes TEXT NOT NULL,
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoked_by_user_id INTEGER REFERENCES users(id),
    CONSTRAINT ck_agent_keys_expiry CHECK (expires_at > created_at)
);

CREATE TABLE IF NOT EXISTS agent_actions (
    id SERIAL PRIMARY KEY,
    agent_key_id INTEGER NOT NULL REFERENCES agent_keys(id),
    action VARCHAR(48) NOT NULL,
    target_type VARCHAR(32),
    target_id INTEGER,
    summary VARCHAR(300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_agent_actions_agent_key_id ON agent_actions (agent_key_id);

-- The change log is append-only: nobody, including the app, can edit or delete it.
CREATE OR REPLACE FUNCTION agent_actions_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'agent_actions is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_agent_actions_append_only ON agent_actions;
CREATE TRIGGER trg_agent_actions_append_only BEFORE UPDATE OR DELETE ON agent_actions
    FOR EACH ROW EXECUTE FUNCTION agent_actions_append_only();

COMMIT;
