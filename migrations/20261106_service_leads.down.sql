-- Reverses 20261106_service_leads.sql (drops only the two new lead tables).
BEGIN;
DROP TABLE IF EXISTS service_lead_audit;
DROP TABLE IF EXISTS service_leads;
COMMIT;
