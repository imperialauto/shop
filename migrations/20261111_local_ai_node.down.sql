-- DESTRUCTIVE ONCE USED: removes Local Node tables and their audit history.
-- Never run automatically. Prefer turning LOCAL_NODE_ENABLED off (the feature
-- becomes inert and the tables are ignored) or restoring a backup.
-- Approved findings already copied into repair_orders.tech_notes are NOT removed.
BEGIN;
DROP TRIGGER IF EXISTS trg_diagnostic_review_events_append_only ON diagnostic_review_events;
DROP TRIGGER IF EXISTS trg_diagnostic_reviews_insert_guard ON diagnostic_reviews;
DROP TRIGGER IF EXISTS trg_diagnostic_reviews_guard ON diagnostic_reviews;
DROP FUNCTION IF EXISTS diagnostic_review_events_append_only();
DROP FUNCTION IF EXISTS diagnostic_reviews_insert_guard();
DROP FUNCTION IF EXISTS diagnostic_reviews_guard();
DROP TABLE IF EXISTS diagnostic_review_events;
DROP TABLE IF EXISTS local_node_heartbeats;
DROP TABLE IF EXISTS diagnostic_reviews;
COMMIT;
