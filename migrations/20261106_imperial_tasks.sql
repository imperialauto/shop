-- ADDITIVE task store for Imperial AI. Manual migration after approved backup.
-- No current customer, Quo, repair order, project or invoice rows are changed.
BEGIN;
CREATE TABLE IF NOT EXISTS imperial_tasks (
 id SERIAL PRIMARY KEY,
 title VARCHAR(200) NOT NULL,
 details TEXT,
 source VARCHAR(32) NOT NULL,
 external_id VARCHAR(128) NOT NULL,
 project_key VARCHAR(64),
 category VARCHAR(32) NOT NULL DEFAULT 'internal',
 priority VARCHAR(12) NOT NULL DEFAULT 'normal',
 status VARCHAR(24) NOT NULL DEFAULT 'open',
 assigned_user_id INTEGER REFERENCES users(id),
 due_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT imperial_task_status_ck CHECK (status IN ('open','in_progress','blocked','done_verified','cancelled')),
 CONSTRAINT imperial_task_priority_ck CHECK (priority IN ('normal','urgent','safety'))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_imperial_task_source_event ON imperial_tasks(source,external_id);
CREATE INDEX IF NOT EXISTS ix_imperial_tasks_state_due ON imperial_tasks(status,due_at);
CREATE TABLE IF NOT EXISTS imperial_task_audit (
 id SERIAL PRIMARY KEY,
 task_id INTEGER NOT NULL REFERENCES imperial_tasks(id),
 actor VARCHAR(64) NOT NULL,
 action VARCHAR(32) NOT NULL,
 event_id VARCHAR(128) NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_imperial_task_audit_task_id ON imperial_task_audit(task_id);
COMMIT;
