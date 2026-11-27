-- ADDITIVE ONLY: the Email page's copy of the business mailbox (app/mail_model.py).
-- Gmail stays the record; these four new tables are a copy for lists, search, sorting and customer links.
-- No existing table is changed. The startup check skips these tables until MAIL_INBOX_ENABLED=true, so
-- apply this BEFORE setting that variable. Run with owner approval, after a fresh backup. Safe to run twice.
BEGIN;
CREATE TABLE IF NOT EXISTS mail_accounts (
    id SERIAL PRIMARY KEY,
    address VARCHAR(255) NOT NULL UNIQUE,
    provider VARCHAR(16) NOT NULL DEFAULT 'gmail',
    token_encrypted TEXT,
    history_id VARCHAR(32),
    status VARCHAR(16) NOT NULL DEFAULT 'connected',
    last_sync_at TIMESTAMP WITH TIME ZONE,
    last_error VARCHAR(255),
    connected_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mail_threads (
    id SERIAL PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES mail_accounts(id),
    gmail_thread_id VARCHAR(64) NOT NULL,
    subject VARCHAR(512) NOT NULL DEFAULT '',
    last_from VARCHAR(255) NOT NULL DEFAULT '',
    last_at TIMESTAMP WITH TIME ZONE,
    snippet TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    unread BOOLEAN NOT NULL DEFAULT FALSE,
    in_inbox BOOLEAN NOT NULL DEFAULT TRUE,
    starred BOOLEAN NOT NULL DEFAULT FALSE,
    category VARCHAR(16) NOT NULL DEFAULT 'other',
    category_locked BOOLEAN NOT NULL DEFAULT FALSE,
    urgent BOOLEAN NOT NULL DEFAULT FALSE,
    customer_id INTEGER REFERENCES customers(id),
    repair_order_id INTEGER REFERENCES repair_orders(id),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_threads_account_thread ON mail_threads (account_id, gmail_thread_id);
CREATE INDEX IF NOT EXISTS ix_mail_threads_list ON mail_threads (account_id, in_inbox, category, last_at);
CREATE TABLE IF NOT EXISTS mail_messages (
    id SERIAL PRIMARY KEY,
    thread_id INTEGER NOT NULL REFERENCES mail_threads(id),
    gmail_message_id VARCHAR(64) NOT NULL UNIQUE,
    rfc_message_id VARCHAR(512),
    from_name VARCHAR(255) NOT NULL DEFAULT '',
    from_address VARCHAR(255) NOT NULL DEFAULT '',
    reply_to VARCHAR(512) NOT NULL DEFAULT '',
    to_addresses TEXT NOT NULL DEFAULT '',
    cc_addresses TEXT NOT NULL DEFAULT '',
    subject VARCHAR(512) NOT NULL DEFAULT '',
    sent_at TIMESTAMP WITH TIME ZONE,
    snippet TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    body_html TEXT,
    labels TEXT NOT NULL DEFAULT '[]',
    outgoing BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_mail_messages_thread_id ON mail_messages (thread_id);
CREATE TABLE IF NOT EXISTS mail_attachments (
    id SERIAL PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES mail_messages(id),
    gmail_attachment_id TEXT NOT NULL,
    filename VARCHAR(255) NOT NULL DEFAULT 'attachment',
    mime_type VARCHAR(128) NOT NULL DEFAULT 'application/octet-stream',
    size INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_mail_attachments_message_id ON mail_attachments (message_id);
COMMIT;
