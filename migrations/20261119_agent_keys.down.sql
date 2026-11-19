-- DESTRUCTIVE ONCE USED: removes assistant keys and their change log.
-- Never run automatically. Prefer revoking keys in Settings, or setting
-- AGENT_API_ENABLED=false (every key then stops working and the tables are ignored).
-- Customers, vehicles, jobs and appointments the assistant added are NOT removed.
BEGIN;
DROP TRIGGER IF EXISTS trg_agent_actions_append_only ON agent_actions;
DROP FUNCTION IF EXISTS agent_actions_append_only();
DROP TABLE IF EXISTS agent_actions;
DROP TABLE IF EXISTS agent_keys;
COMMIT;
