-- ============================================================================
-- MIGRATION: Fix kyc_verifications for proper KYC storage
-- Run this in Supabase SQL Editor AFTER 001_initial_schema.sql
-- ============================================================================

-- Add kyc_data JSONB column to store full Aadhaar/PAN/DL data
-- (instead of storing it in Supabase user_metadata which inflates the JWT)
ALTER TABLE kyc_verifications
  ADD COLUMN IF NOT EXISTS kyc_data JSONB;

-- Add unique constraint on verification_id for ON CONFLICT upsert
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'kyc_verifications_verification_id_key'
  ) THEN
    ALTER TABLE kyc_verifications
      ADD CONSTRAINT kyc_verifications_verification_id_key UNIQUE (verification_id);
  END IF;
END $$;

-- Index for quick user_id lookups
CREATE INDEX IF NOT EXISTS idx_kyc_verifications_user_id ON kyc_verifications(user_id);
