-- ROLLBACK ONLY, requires explicit owner approval. Deletes journey history.
-- Does not affect repair_orders or any existing customer/job data.
DROP TABLE IF EXISTS journey_events;
