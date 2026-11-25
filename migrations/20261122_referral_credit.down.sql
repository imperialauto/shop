-- Removes the referral tables. Deletes every referral code, referral and store credit record.
-- Requires explicit owner approval. Discount and store-credit lines already on jobs stay as they are.
BEGIN;
DROP TABLE IF EXISTS customer_credits;
DROP TABLE IF EXISTS referrals;
DROP TABLE IF EXISTS referral_codes;
COMMIT;
