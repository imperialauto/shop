-- ADDITIVE ONLY: Imperial Operations' owner-only development work queue.
-- Requires explicit owner database-migration approval before applying.
-- No background worker, AI API usage or outbound SMS is enabled by this migration.

BEGIN;

CREATE TABLE IF NOT EXISTS dev_work_jobs (
    id SERIAL PRIMARY KEY,
    source VARCHAR(32) NOT NULL,
    external_id VARCHAR(128) NOT NULL,
    project_key VARCHAR(32) NOT NULL,
    instruction TEXT NOT NULL,
    model_preference VARCHAR(16) NOT NULL DEFAULT 'auto',
    status VARCHAR(24) NOT NULL DEFAULT 'queued',
    assigned_worker VARCHAR(64),
    latest_summary TEXT,
    result_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_dev_work_source_external UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS ix_dev_work_status_created ON dev_work_jobs (status, created_at);

CREATE TABLE IF NOT EXISTS dev_work_events (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES dev_work_jobs(id),
    actor VARCHAR(32) NOT NULL,
    action VARCHAR(32) NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_dev_work_events_job_id ON dev_work_events (job_id);

COMMIT;
