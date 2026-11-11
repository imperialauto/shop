-- Add worker lease/retry tracking. Apply only after the foundation migration
-- and explicit owner backup/rollback approval. No worker starts from this SQL.
BEGIN;
ALTER TABLE dev_work_jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dev_work_jobs ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ;
ALTER TABLE dev_work_jobs ADD COLUMN IF NOT EXISTS retry_after TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS dev_work_notices (
    id SERIAL PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE REFERENCES dev_work_events(id),
    job_id INTEGER NOT NULL REFERENCES dev_work_jobs(id),
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    text TEXT NOT NULL,
    claim_nonce VARCHAR(64),
    claimed_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_dev_work_notices_job_id ON dev_work_notices(job_id);
COMMIT;
