-- ADDITIVE ONLY: referral codes, referrals and store credit for the $25 + $25 referral offer.
-- Requires explicit owner approval before applying to any non-disposable database.
-- Creates three new tables. Does not alter or read any existing table's data. Safe to run twice.
-- After applying, set REFERRALS_ENABLED=true on the web service to turn the feature on.

BEGIN;

CREATE TABLE IF NOT EXISTS referral_codes (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    code VARCHAR(16) NOT NULL UNIQUE,
    created_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    code VARCHAR(16) NOT NULL,
    referrer_customer_id INTEGER NOT NULL REFERENCES customers(id),
    friend_customer_id INTEGER NOT NULL UNIQUE REFERENCES customers(id),
    status VARCHAR(12) NOT NULL DEFAULT 'pending',
    friend_ro_id INTEGER REFERENCES repair_orders(id),
    discount_line_id INTEGER REFERENCES line_items(id),
    created_by VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    earned_at TIMESTAMPTZ,
    CONSTRAINT ck_referrals_not_self CHECK (referrer_customer_id <> friend_customer_id)
);
CREATE INDEX IF NOT EXISTS ix_referrals_referrer_customer_id ON referrals (referrer_customer_id);

CREATE TABLE IF NOT EXISTS customer_credits (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount DOUBLE PRECISION NOT NULL,
    kind VARCHAR(16) NOT NULL,
    referral_id INTEGER REFERENCES referrals(id),
    repair_order_id INTEGER REFERENCES repair_orders(id),
    line_item_id INTEGER REFERENCES line_items(id),
    actor VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_customer_credits_customer_id ON customer_credits (customer_id);
-- One earned credit per referral, even if a payment is recorded twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_credits_referral ON customer_credits (referral_id) WHERE kind = 'referral';

COMMIT;
