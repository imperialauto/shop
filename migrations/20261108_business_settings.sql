-- Owner-editable business settings and their change history.
BEGIN;
CREATE TABLE IF NOT EXISTS business_settings (
  key VARCHAR(64) PRIMARY KEY,
  value TEXT NOT NULL,
  updated_by VARCHAR(64) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS business_setting_changes (
  id SERIAL PRIMARY KEY,
  key VARCHAR(64) NOT NULL,
  old_value TEXT,
  new_value TEXT NOT NULL,
  changed_by VARCHAR(64) NOT NULL,
  changed_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_business_setting_changes_key ON business_setting_changes (key);
COMMIT;
