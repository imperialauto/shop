-- Ask Imperial (estimate drafts on the Local Node) and staff-chosen shop rules.
-- Additive only: new tables, nothing existing is altered. Safe to run twice.
-- Run once, with owner approval, after a fresh backup, before setting ASK_IMPERIAL_ENABLED=true.
-- Requires migrations/20261111_local_ai_node.sql (same worker) and 20261115_ro_line_completion_attachments.sql.
BEGIN;

CREATE TABLE IF NOT EXISTS assist_requests (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(24) NOT NULL DEFAULT 'estimate_draft' CONSTRAINT ck_assist_kind CHECK (kind IN ('estimate_draft')),
    status VARCHAR(16) NOT NULL DEFAULT 'queued' CONSTRAINT ck_assist_status
        CHECK (status IN ('queued','processing','retry_wait','needs_labor','ready','stale','failed','applied','cancelled')),
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    request_text TEXT NOT NULL,
    source_snapshot TEXT NOT NULL,
    base_fingerprint VARCHAR(64) NOT NULL,
    base_customer_message VARCHAR(128),
    base_summary TEXT,
    prompt_version VARCHAR(32) NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner VARCHAR(64),
    lease_token VARCHAR(64),
    lease_until TIMESTAMPTZ,
    retry_after TIMESTAMPTZ,
    last_error TEXT,
    model_name VARCHAR(96),
    ai_json TEXT,
    stale_reason TEXT,
    applied_line_ids TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_assist_attempts CHECK (attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10)
);
CREATE INDEX IF NOT EXISTS ix_assist_requests_repair_order_id ON assist_requests (repair_order_id);
CREATE INDEX IF NOT EXISTS ix_assist_status_created ON assist_requests (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_assist_one_open_per_ro ON assist_requests (repair_order_id)
    WHERE status IN ('queued','processing','retry_wait','needs_labor','ready','stale','failed');

CREATE TABLE IF NOT EXISTS assist_events (
    id SERIAL PRIMARY KEY,
    request_id INTEGER REFERENCES assist_requests(id),
    repair_order_id INTEGER REFERENCES repair_orders(id),
    actor VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_assist_events_request_id ON assist_events (request_id);
CREATE INDEX IF NOT EXISTS ix_assist_events_repair_order_id ON assist_events (repair_order_id);

CREATE TABLE IF NOT EXISTS assist_labor_evidence (
    id SERIAL PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES assist_requests(id),
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    vehicle_config TEXT NOT NULL,
    operation TEXT NOT NULL,
    source VARCHAR(40) NOT NULL,
    guide_hours NUMERIC(6,2) NOT NULL CONSTRAINT ck_labor_hours_range CHECK (guide_hours > 0 AND guide_hours <= 100),
    inclusions TEXT,
    overlap TEXT,
    rate_type VARCHAR(12) NOT NULL DEFAULT 'shop' CONSTRAINT ck_labor_rate_type CHECK (rate_type IN ('shop','mobile','diesel','cspr')),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_by_user_id INTEGER NOT NULL REFERENCES users(id),
    shop_manager_ref VARCHAR(64),
    parts_json TEXT NOT NULL DEFAULT '[]',
    superseded_by_id INTEGER REFERENCES assist_labor_evidence(id)
);
CREATE INDEX IF NOT EXISTS ix_assist_labor_evidence_request_id ON assist_labor_evidence (request_id);

CREATE TABLE IF NOT EXISTS shop_rules (
    id SERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    topic VARCHAR(40) NOT NULL DEFAULT 'estimates',
    created_by_user_id INTEGER NOT NULL REFERENCES users(id),
    from_request_id INTEGER REFERENCES assist_requests(id),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Evidence and history are append-only: no deletes from the app role's normal paths.
CREATE OR REPLACE FUNCTION assist_no_delete() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Ask Imperial history and labor evidence are kept; % rows cannot be deleted', TG_TABLE_NAME;
END $$;
DROP TRIGGER IF EXISTS trg_assist_events_no_delete ON assist_events;
CREATE TRIGGER trg_assist_events_no_delete BEFORE DELETE ON assist_events FOR EACH ROW EXECUTE FUNCTION assist_no_delete();
DROP TRIGGER IF EXISTS trg_assist_labor_no_delete ON assist_labor_evidence;
CREATE TRIGGER trg_assist_labor_no_delete BEFORE DELETE ON assist_labor_evidence FOR EACH ROW EXECUTE FUNCTION assist_no_delete();

COMMIT;
