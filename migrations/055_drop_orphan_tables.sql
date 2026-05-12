-- 055: Drop verified-orphan tables
--
-- billing_history    : was subscription billing (subscriptions removed in 054)
-- trial_eligibility  : was subscription trial gate (subscriptions removed in 054)
-- item_profitability : never populated by any background job; endpoint
--                      computes profitability live from order_items + recipes
-- daily_pnl          : never populated by any background job; endpoint removed

BEGIN;

DROP TABLE IF EXISTS billing_history CASCADE;
DROP TABLE IF EXISTS trial_eligibility CASCADE;
DROP TABLE IF EXISTS item_profitability CASCADE;
DROP TABLE IF EXISTS daily_pnl CASCADE;

COMMIT;
