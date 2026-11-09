-- Where a customer's name came from (staff, quo_contact, text, call, call summary) and when Imperial last looked for one.
BEGIN;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS name_source VARCHAR(24);
ALTER TABLE customers ADD COLUMN IF NOT EXISTS name_checked_at TIMESTAMPTZ;
COMMIT;
