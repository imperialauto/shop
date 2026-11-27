-- Undo 20261127_uhaul_flow.sql. Drops only the two U-Haul tables (and what's in them). Jobs, customers and
-- vehicles that U-Haul requests opened stay. Set UHAUL_ENABLED back to false first. Run with owner approval.
BEGIN;
DROP TABLE IF EXISTS uhaul_payments;
DROP TABLE IF EXISTS uhaul_requests;
COMMIT;
