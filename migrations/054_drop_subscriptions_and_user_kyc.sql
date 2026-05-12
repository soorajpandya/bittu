-- ============================================================================
-- Migration 054 — Drop subscription engine and legacy user-level KYC tables.
--
-- Context:
--   * Subscription product was removed from the API surface in commits
--     6149015 (router) and the subsequent middleware/service strip.
--   * Cashfree-driven user-level kyc_verifications table was only consumed by
--     the now-deleted /digilocker router and digilocker_service.py.
--
-- Tables retained (still active under /admin/merchant-kyc):
--   merchant_kyc_profiles, merchant_kyc_documents, merchant_kyc_owners,
--   merchant_kyc_bank_accounts, merchant_kyc_audit_events.
--
-- This migration is irreversible — the dropped tables hold zero rows referenced
-- by any live code path.
-- ============================================================================

BEGIN;

-- 1. Subscription engine tables (created in 001_initial_schema.sql).
DROP TABLE IF EXISTS user_subscriptions CASCADE;
DROP TABLE IF EXISTS subscription_plans CASCADE;

-- Optional satellite tables that were created by later migrations for the
-- subscription product. We use IF EXISTS so the migration is safe even if
-- some envs never created them.
DROP TABLE IF EXISTS subscription_addons         CASCADE;
DROP TABLE IF EXISTS subscription_addon_purchases CASCADE;
DROP TABLE IF EXISTS subscription_invoices       CASCADE;
DROP TABLE IF EXISTS subscription_events         CASCADE;
DROP TABLE IF EXISTS subscription_payment_history CASCADE;

-- 2. Legacy user-level KYC table (separate from merchant_kyc_*).
DROP TABLE IF EXISTS kyc_verifications CASCADE;

COMMIT;
