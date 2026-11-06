-- Lets the owner turn a login off without losing its history, and forces a
-- new password after a temporary one.
BEGIN;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;
COMMIT;
