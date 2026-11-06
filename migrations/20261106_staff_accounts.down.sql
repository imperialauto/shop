-- Reverses 20261106_staff_accounts.sql.
BEGIN;
ALTER TABLE users DROP COLUMN IF EXISTS must_change_password;
ALTER TABLE users DROP COLUMN IF EXISTS is_active;
COMMIT;
