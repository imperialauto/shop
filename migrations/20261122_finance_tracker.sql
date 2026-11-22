-- ADDITIVE money tracker for the owner's Finance page. Manual migration after an approved backup.
-- Four new, empty tables. No customer, job, invoice, payment or settings rows are changed.
BEGIN;
CREATE TABLE IF NOT EXISTS finance_accounts (
 id SERIAL PRIMARY KEY,
 name VARCHAR(80) NOT NULL UNIQUE,
 scope VARCHAR(12) NOT NULL DEFAULT 'business',
 balance_cents INTEGER NOT NULL DEFAULT 0,
 held_cents INTEGER NOT NULL DEFAULT 0,
 note VARCHAR(200),
 as_of DATE NOT NULL,
 active BOOLEAN NOT NULL DEFAULT TRUE,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT finance_account_scope_ck CHECK (scope IN ('business','personal'))
);
CREATE TABLE IF NOT EXISTS finance_bills (
 id SERIAL PRIMARY KEY,
 name VARCHAR(80) NOT NULL,
 scope VARCHAR(12) NOT NULL DEFAULT 'business',
 category VARCHAR(16) NOT NULL DEFAULT 'other',
 amount_cents INTEGER NOT NULL,
 due_date DATE NOT NULL,
 repeats VARCHAR(8) NOT NULL DEFAULT 'once',
 status VARCHAR(8) NOT NULL DEFAULT 'open',
 paid_on DATE,
 note VARCHAR(200),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT finance_bill_scope_ck CHECK (scope IN ('business','personal')),
 CONSTRAINT finance_bill_status_ck CHECK (status IN ('open','paid','skipped')),
 CONSTRAINT finance_bill_repeats_ck CHECK (repeats IN ('once','weekly','monthly'))
);
CREATE INDEX IF NOT EXISTS ix_finance_bills_status_due ON finance_bills(status, due_date);
CREATE TABLE IF NOT EXISTS finance_incoming (
 id SERIAL PRIMARY KEY,
 source VARCHAR(80) NOT NULL,
 scope VARCHAR(12) NOT NULL DEFAULT 'business',
 amount_cents INTEGER NOT NULL,
 expected_date DATE,
 status VARCHAR(10) NOT NULL DEFAULT 'expected',
 received_on DATE,
 note VARCHAR(200),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT finance_incoming_scope_ck CHECK (scope IN ('business','personal')),
 CONSTRAINT finance_incoming_status_ck CHECK (status IN ('expected','received','dropped'))
);
CREATE INDEX IF NOT EXISTS ix_finance_incoming_status_date ON finance_incoming(status, expected_date);
CREATE TABLE IF NOT EXISTS finance_changes (
 id SERIAL PRIMARY KEY,
 actor VARCHAR(64) NOT NULL,
 action VARCHAR(32) NOT NULL,
 detail TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
COMMIT;
