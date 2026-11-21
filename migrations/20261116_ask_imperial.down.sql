-- DESTRUCTIVE ONCE USED: removes Ask Imperial requests, labor evidence and shop rules.
-- Never run automatically. Export the four tables first if any were used.
BEGIN;
DROP TABLE IF EXISTS shop_rules;
DROP TABLE IF EXISTS assist_labor_evidence;
DROP TABLE IF EXISTS assist_events;
DROP TABLE IF EXISTS assist_requests;
DROP FUNCTION IF EXISTS assist_no_delete();
COMMIT;
