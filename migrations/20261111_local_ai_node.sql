-- ADDITIVE ONLY: Imperial Local Node (shop-workstation AI diagnostic drafts).
-- Requires explicit owner approval before applying to any non-disposable database.
-- Creates three new tables, constraints and triggers. Does not alter or read
-- any existing table's data. Safe to run twice.

BEGIN;

CREATE TABLE IF NOT EXISTS diagnostic_reviews (
    id SERIAL PRIMARY KEY,
    repair_order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    vehicle_id INTEGER REFERENCES vehicles(id),
    requested_by_user_id INTEGER NOT NULL REFERENCES users(id),
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    source_snapshot TEXT NOT NULL,
    prompt_version VARCHAR(32) NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner VARCHAR(64),
    lease_token VARCHAR(64),
    lease_until TIMESTAMPTZ,
    retry_after TIMESTAMPTZ,
    last_error TEXT,
    model_name VARCHAR(96),
    draft_json TEXT,
    validation_notes TEXT,
    reviewed_by_user_id INTEGER REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    approved_text TEXT,
    decision_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ck_diag_review_status CHECK (status IN ('queued','processing','retry_wait','draft_ready','failed','approved','rejected','cancelled')),
    CONSTRAINT ck_diag_review_reviewer CHECK (status NOT IN ('approved','rejected') OR (reviewed_by_user_id IS NOT NULL AND reviewed_at IS NOT NULL)),
    CONSTRAINT ck_diag_review_approved_text CHECK (status <> 'approved' OR (approved_text IS NOT NULL AND length(approved_text) > 0)),
    CONSTRAINT ck_diag_review_attempts CHECK (attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10)
);
CREATE INDEX IF NOT EXISTS ix_diagnostic_reviews_repair_order_id ON diagnostic_reviews (repair_order_id);
CREATE INDEX IF NOT EXISTS ix_diag_review_status_created ON diagnostic_reviews (status, created_at);
-- One in-flight review per job.
CREATE UNIQUE INDEX IF NOT EXISTS uq_diag_review_one_active_per_ro ON diagnostic_reviews (repair_order_id)
    WHERE status IN ('queued','processing','retry_wait');

CREATE TABLE IF NOT EXISTS diagnostic_review_events (
    id SERIAL PRIMARY KEY,
    review_id INTEGER NOT NULL REFERENCES diagnostic_reviews(id),
    actor VARCHAR(64) NOT NULL,
    action VARCHAR(32) NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_diagnostic_review_events_review_id ON diagnostic_review_events (review_id);

CREATE TABLE IF NOT EXISTS local_node_heartbeats (
    worker_id VARCHAR(64) PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    version VARCHAR(32),
    model_name VARCHAR(96),
    ollama_ok VARCHAR(8),
    detail TEXT
);

-- Database-level approval rules (in addition to the application checks):
--  * only a draft_ready row can become approved/rejected
--  * an approved row must carry the validated AI draft it was based on
--  * approved/rejected/cancelled rows are final and cannot be edited
CREATE OR REPLACE FUNCTION diagnostic_reviews_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'diagnostic_reviews rows are never deleted';
    END IF;
    IF OLD.status IN ('approved','rejected','cancelled') THEN
        RAISE EXCEPTION 'diagnostic review % is final (%)', OLD.id, OLD.status;
    END IF;
    IF NEW.status IN ('approved','rejected') AND OLD.status <> 'draft_ready' THEN
        RAISE EXCEPTION 'only a draft_ready review can be approved or rejected';
    END IF;
    IF NEW.status = 'approved' AND NEW.draft_json IS NULL THEN
        RAISE EXCEPTION 'cannot approve a review without a validated draft';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_diagnostic_reviews_guard ON diagnostic_reviews;
CREATE TRIGGER trg_diagnostic_reviews_guard BEFORE UPDATE OR DELETE ON diagnostic_reviews
    FOR EACH ROW EXECUTE FUNCTION diagnostic_reviews_guard();

-- New rows always start as queued (nobody can insert a pre-approved review).
CREATE OR REPLACE FUNCTION diagnostic_reviews_insert_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.status <> 'queued' OR NEW.reviewed_by_user_id IS NOT NULL OR NEW.approved_text IS NOT NULL THEN
        RAISE EXCEPTION 'new diagnostic reviews must start as queued with no decision';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_diagnostic_reviews_insert_guard ON diagnostic_reviews;
CREATE TRIGGER trg_diagnostic_reviews_insert_guard BEFORE INSERT ON diagnostic_reviews
    FOR EACH ROW EXECUTE FUNCTION diagnostic_reviews_insert_guard();

-- Audit history is append-only.
CREATE OR REPLACE FUNCTION diagnostic_review_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'diagnostic_review_events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_diagnostic_review_events_append_only ON diagnostic_review_events;
CREATE TRIGGER trg_diagnostic_review_events_append_only BEFORE UPDATE OR DELETE ON diagnostic_review_events
    FOR EACH ROW EXECUTE FUNCTION diagnostic_review_events_append_only();

COMMIT;
