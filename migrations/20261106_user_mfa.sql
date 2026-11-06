-- Apply BEFORE deploying this code. Additive and repeatable.
-- Reverse with 20261106_user_mfa.down.sql.
BEGIN;
CREATE TABLE IF NOT EXISTS user_mfa (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    secret_encrypted TEXT NOT NULL,
    recovery_hashes TEXT NOT NULL DEFAULT '[]',
    last_counter BIGINT NOT NULL DEFAULT 0,
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
