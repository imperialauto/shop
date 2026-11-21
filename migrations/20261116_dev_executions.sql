-- Additive; apply only to approved isolated QA until release authorization.
CREATE TABLE IF NOT EXISTS dev_executions (
 id SERIAL PRIMARY KEY,
 job_id INTEGER NOT NULL UNIQUE REFERENCES dev_work_jobs(id),
 worker_id VARCHAR(64) NOT NULL,
 mode VARCHAR(16) NOT NULL,
 base_sha VARCHAR(40) NOT NULL,
 state VARCHAR(24) NOT NULL DEFAULT 'approved',
 claim_id VARCHAR(64) UNIQUE,
 token_hash VARCHAR(64),
 lease_until TIMESTAMPTZ,
 deadline TIMESTAMPTZ,
 evidence TEXT
);
-- One running job per host, including concurrent claims for different jobs.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dev_running_host ON dev_executions(worker_id) WHERE state = 'running';
