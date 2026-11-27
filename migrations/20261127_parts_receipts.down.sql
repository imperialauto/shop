-- Reverses 20261127_parts_receipts.sql. Deletes every saved parts receipt: take a backup first.
BEGIN;
DROP TABLE IF EXISTS parts_receipts;
COMMIT;
