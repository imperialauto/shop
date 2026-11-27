-- ADDITIVE bank feed for the owner's Money page (Plaid). Manual migration after an approved backup.
-- Three new, empty tables. No customer, job, invoice, payment, settings or money-tracker rows change.
-- Safe to run more than once.
BEGIN;
CREATE TABLE IF NOT EXISTS bank_links (
 id SERIAL PRIMARY KEY,
 item_id VARCHAR(100) NOT NULL UNIQUE,
 institution VARCHAR(120) NOT NULL DEFAULT 'Bank',
 access_token_enc TEXT NOT NULL,
 cursor TEXT,
 status VARCHAR(16) NOT NULL DEFAULT 'ok',
 last_synced_at TIMESTAMPTZ,
 last_error VARCHAR(200),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT bank_link_status_ck CHECK (status IN ('ok','needs_login','error'))
);
CREATE TABLE IF NOT EXISTS bank_accounts (
 id SERIAL PRIMARY KEY,
 link_id INTEGER NOT NULL,
 plaid_account_id VARCHAR(100) NOT NULL UNIQUE,
 name VARCHAR(120) NOT NULL,
 mask VARCHAR(8),
 kind VARCHAR(24) NOT NULL DEFAULT 'depository',
 subtype VARCHAR(32),
 finance_account_id INTEGER,
 scope VARCHAR(12) NOT NULL DEFAULT 'business',
 tracked BOOLEAN NOT NULL DEFAULT TRUE,
 current_cents INTEGER,
 available_cents INTEGER,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT bank_account_scope_ck CHECK (scope IN ('business','personal'))
);
CREATE INDEX IF NOT EXISTS ix_bank_accounts_link_id ON bank_accounts(link_id);
CREATE TABLE IF NOT EXISTS bank_transactions (
 id SERIAL PRIMARY KEY,
 plaid_transaction_id VARCHAR(100) NOT NULL UNIQUE,
 bank_account_id INTEGER NOT NULL,
 day DATE NOT NULL,
 name VARCHAR(160) NOT NULL,
 amount_cents INTEGER NOT NULL,
 pending BOOLEAN NOT NULL DEFAULT FALSE,
 category VARCHAR(60),
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_bank_transactions_bank_account_id ON bank_transactions(bank_account_id);
CREATE INDEX IF NOT EXISTS ix_bank_transactions_day ON bank_transactions(day);
COMMIT;
