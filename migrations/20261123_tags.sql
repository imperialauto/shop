-- ADDITIVE ONLY: short tags on jobs and customers, shown on cards and searchable.
-- Existing jobs and customers are untouched. The app does NOT create tables on start,
-- so apply this before or right after deploy. Run with owner approval, after a fresh backup. Safe to run twice.
BEGIN;
CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(12) NOT NULL CHECK (kind IN ('job', 'customer')),
    target_id INTEGER NOT NULL,
    name VARCHAR(30) NOT NULL,
    created_by VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_tags_kind_target_name UNIQUE (kind, target_id, name)
);
CREATE INDEX IF NOT EXISTS ix_tags_name ON tags (name);
COMMIT;
