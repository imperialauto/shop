-- P0 persistent service opportunities, separate from short-lived dispatch tasks.
-- Apply ONLY after production database backup/restore verification and owner approval.
-- Enable SERVICE_LEADS_ENABLED=true only after this migration and health checks.
-- No existing customer, appointment, repair order, Quo task or dispatch row is deleted.
BEGIN;
CREATE TABLE IF NOT EXISTS service_leads (
 id SERIAL PRIMARY KEY,
 source VARCHAR(20) NOT NULL,
 external_thread_id VARCHAR(128) NOT NULL,
 customer_phone VARCHAR(32),
 customer_id INTEGER REFERENCES customers(id),
 vehicle_id INTEGER REFERENCES vehicles(id),
 repair_order_id INTEGER REFERENCES repair_orders(id),
 appointment_id INTEGER REFERENCES appointments(id),
 last_dispatch_item_id INTEGER REFERENCES dispatch_items(id),
 assigned_user_id INTEGER REFERENCES users(id),
 status VARCHAR(32) NOT NULL DEFAULT 'new',
 category VARCHAR(32) NOT NULL DEFAULT 'general',
 urgency VARCHAR(12) NOT NULL DEFAULT 'normal',
 summary TEXT NOT NULL DEFAULT '',
 next_action TEXT NOT NULL DEFAULT '',
 next_action_due_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_customer_activity_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 last_event_id VARCHAR(128),
 closure_reason VARCHAR(128),
 closure_evidence_ref VARCHAR(128),
 closed_by_user_id INTEGER REFERENCES users(id),
 resolved_at TIMESTAMPTZ,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT service_leads_status_ck CHECK (
   status IN ('new','information_needed','estimate_needed','estimate_sent','awaiting_customer','scheduling','booked','declined','closed_exception')
 ),
 CONSTRAINT service_leads_urgency_ck CHECK (urgency IN ('normal','urgent','safety')),
 CONSTRAINT service_leads_resolution_ck CHECK (
   (status IN ('new','information_needed','estimate_needed','estimate_sent','awaiting_customer','scheduling') AND resolved_at IS NULL)
   OR (status IN ('booked','declined','closed_exception') AND resolved_at IS NOT NULL)
 )
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_service_leads_active_source_thread
 ON service_leads (source, external_thread_id)
 WHERE status IN ('new','information_needed','estimate_needed','estimate_sent','awaiting_customer','scheduling');
CREATE INDEX IF NOT EXISTS ix_service_leads_follow_up ON service_leads(status, next_action_due_at);
CREATE TABLE IF NOT EXISTS service_lead_audit (
 id SERIAL PRIMARY KEY,
 lead_id INTEGER NOT NULL REFERENCES service_leads(id),
 action VARCHAR(32) NOT NULL,
 actor VARCHAR(64) NOT NULL,
 from_status VARCHAR(32),
 to_status VARCHAR(32),
 evidence_ref VARCHAR(128),
 at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_service_lead_audit_lead ON service_lead_audit(lead_id);
COMMIT;
