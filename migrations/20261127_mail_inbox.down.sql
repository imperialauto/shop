-- Reverses 20261127_mail_inbox.sql. Removes only the dashboard's copy; nothing in Gmail changes.
-- Set MAIL_INBOX_ENABLED back to false first.
BEGIN;
DROP TABLE IF EXISTS mail_attachments;
DROP TABLE IF EXISTS mail_messages;
DROP TABLE IF EXISTS mail_threads;
DROP TABLE IF EXISTS mail_accounts;
COMMIT;
