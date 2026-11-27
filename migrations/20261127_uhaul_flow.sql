-- ADDITIVE ONLY: U-Haul requests (roadside offers and repair POs from U-Haul's emails) and the payments
-- matched to them from the vendor portal's RPO History. Existing jobs, invoices and payments are untouched.
-- The app does NOT create tables on start. Apply this, then set UHAUL_ENABLED=true; until both, the U-Haul
-- page says it isn't switched on and the startup check skips these tables.
-- Run with owner approval, after a fresh backup. Safe to run twice.
BEGIN;
CREATE TABLE IF NOT EXISTS uhaul_requests (
    id SERIAL PRIMARY KEY,
    kind VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'new',
    message_id VARCHAR(200) UNIQUE,
    thread_id VARCHAR(200),
    sender VARCHAR(200),
    subject VARCHAR(300),
    received_at TIMESTAMPTZ,
    customer_name VARCHAR(128),
    offer_url VARCHAR(500),
    rpo_number VARCHAR(32),
    hotline_ref VARCHAR(32),
    equipment_number VARCHAR(48),
    vin VARCHAR(17),
    plate VARCHAR(32),
    year_make VARCHAR(80),
    location TEXT,
    action_date DATE,
    repair_by DATE,
    estimated_amount DOUBLE PRECISION,
    reason TEXT,
    cause_text VARCHAR(200),
    rdm_email VARCHAR(128),
    review_reason TEXT,
    resend_count INTEGER NOT NULL DEFAULT 0,
    last_seen_at TIMESTAMPTZ,
    repair_order_id INTEGER REFERENCES repair_orders(id) ON DELETE SET NULL,
    decided_at TIMESTAMPTZ,
    decided_by VARCHAR(64),
    en_route_at TIMESTAMPTZ,
    on_site_at TIMESTAMPTZ,
    done_at TIMESTAMPTZ,
    back_at TIMESTAMPTZ,
    odometer INTEGER,
    is_trailer BOOLEAN NOT NULL DEFAULT false,
    travel_basis VARCHAR(8) NOT NULL DEFAULT 'hours',
    travel_miles DOUBLE PRECISION,
    condition TEXT,
    cause TEXT,
    correction TEXT,
    vendor_remarks TEXT,
    ready_at TIMESTAMPTZ,
    submitted_at TIMESTAMPTZ,
    submitted_by VARCHAR(64),
    submitted_invoice VARCHAR(32),
    submitted_amount DOUBLE PRECISION,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_uhaul_requests_thread_id ON uhaul_requests (thread_id);
CREATE INDEX IF NOT EXISTS ix_uhaul_requests_rpo_number ON uhaul_requests (rpo_number);
CREATE INDEX IF NOT EXISTS ix_uhaul_requests_repair_order_id ON uhaul_requests (repair_order_id);
CREATE TABLE IF NOT EXISTS uhaul_payments (
    id SERIAL PRIMARY KEY,
    request_id INTEGER REFERENCES uhaul_requests(id) ON DELETE SET NULL,
    rpo_number VARCHAR(32) NOT NULL,
    paid_on DATE,
    reference VARCHAR(64),
    amount DOUBLE PRECISION NOT NULL,
    recorded_by VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_uhaul_payments_request_id ON uhaul_payments (request_id);
CREATE INDEX IF NOT EXISTS ix_uhaul_payments_rpo_number ON uhaul_payments (rpo_number);
COMMIT;
