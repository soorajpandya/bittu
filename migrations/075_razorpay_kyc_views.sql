-- ============================================================================
-- 075_razorpay_kyc_views.sql
--
-- Clean read-surfaces over rzp_kyc_submissions:
--
--   1. v_rzp_kyc_csv_export        — exactly the 9 columns Razorpay's Batch
--                                    Upload template expects, restricted to
--                                    rows that still need to be uploaded.
--                                    Export this view from Supabase Studio
--                                    to get a Razorpay-compatible CSV.
--
--   2. v_rzp_account_mapping       — merchant_id ↔ razorpay_account_id ↔
--                                    lifecycle status. Join this view to
--                                    restaurants / merchant_ledger / orders
--                                    to reconcile linked accounts.
--
-- No data movement, no column drops. Service code keeps using the base
-- table; humans use the views.
-- ============================================================================

-- ── 1) The CSV export surface ──────────────────────────────────────────────
-- Only rows that are still queued (or already in a generated batch file but
-- not yet uploaded to Razorpay). Once a row is UPLOADED_TO_RAZORPAY /
-- APPROVED / REJECTED it disappears from this view.
DROP VIEW IF EXISTS v_rzp_kyc_csv_export;
CREATE VIEW v_rzp_kyc_csv_export AS
SELECT
    account_name,
    account_email,
    dashboard_access,
    customer_refunds,
    business_name,
    business_type,
    ifsc_code,
    account_number,
    beneficiary_name
FROM rzp_kyc_submissions
WHERE status IN ('PENDING_BATCH_UPLOAD', 'IN_BATCH_FILE')
ORDER BY created_at;

COMMENT ON VIEW v_rzp_kyc_csv_export IS
  'Exact 9-column Razorpay batch-upload template. Export to CSV and upload '
  'directly on Razorpay Dashboard → Batch Uploads → Linked Account Creation.';


-- ── 2) Merchant ↔ Razorpay account mapping surface ─────────────────────────
DROP VIEW IF EXISTS v_rzp_account_mapping;
CREATE VIEW v_rzp_account_mapping AS
SELECT
    s.id                              AS submission_id,
    s.merchant_id,
    s.razorpay_account_id,
    s.razorpay_account_status,
    s.status                          AS lifecycle_status,
    s.batch_id,
    b.batch_no,
    b.slot_at                         AS batch_slot_at,
    b.status                          AS batch_status,
    s.account_name,
    s.account_email,
    s.business_name,
    s.business_type,
    s.ifsc_code,
    s.account_number,
    s.beneficiary_name,
    s.rejection_reason,
    s.created_at,
    s.updated_at,
    s.batch_assigned_at,
    s.approved_at,
    s.rejected_at
FROM rzp_kyc_submissions s
LEFT JOIN rzp_kyc_batches b ON b.id = s.batch_id;

COMMENT ON VIEW v_rzp_account_mapping IS
  'Joinable lookup of merchant_id ↔ razorpay_account_id ↔ lifecycle status. '
  'Use this to reconcile linked accounts against restaurants, '
  'merchant_ledger, orders, etc.';


-- ── Permissions (mirror the base table; Supabase RLS/role grants apply) ────
DO $$ BEGIN
    -- If you have an authenticated/anon role pattern, mirror what the
    -- base table has. No-op if roles don't exist.
    PERFORM 1 FROM pg_roles WHERE rolname = 'authenticated';
    IF FOUND THEN
        EXECUTE 'GRANT SELECT ON v_rzp_kyc_csv_export TO authenticated';
        EXECUTE 'GRANT SELECT ON v_rzp_account_mapping TO authenticated';
    END IF;
EXCEPTION WHEN OTHERS THEN NULL; END $$;
