-- Reverses 20261122_finance_tracker.sql. Drops only the four money-tracker tables (and the figures typed into them).
BEGIN;
DROP TABLE IF EXISTS finance_changes;
DROP TABLE IF EXISTS finance_incoming;
DROP TABLE IF EXISTS finance_bills;
DROP TABLE IF EXISTS finance_accounts;
COMMIT;
