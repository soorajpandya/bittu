-- ════════════════════════════════════════════════════════════════════════════
-- 067 — Settlement: record Razorpay's server-side cut as a first-class field
-- ════════════════════════════════════════════════════════════════════════════
--
-- Context
-- ─────────────────────────────────────────────────────────────────────────────
-- The fee model in `statement_service.py` moved from a 0.30 % all-Bittu cut
-- to a 5.00 % gross deduction split three ways:
--
--   5.00 % = 1.1682 % Razorpay (auto-deducted server-side)
--          + 3.2473 % Bittu platform fee (base)
--          + 0.5845 % GST on the Bittu fee
--
-- Razorpay intercepts its slice BEFORE settling the remainder to our pooled
-- account; we never own those paisa and they must not be booked as Bittu
-- revenue. Storing the cut explicitly keeps the four-way reconciliation
-- (razorpay + bittu_fee + gst + net == gross) provable at the row level and
-- gives us a clean column to join against Razorpay's own settlement files.
--
-- Back-compat
-- ─────────────────────────────────────────────────────────────────────────────
-- Existing settlements created under the 0.30 % model have an implicit
-- razorpay_cut of 0 (we were absorbing PG fees ourselves), so DEFAULT 0 is
-- correct and no data backfill is required. New settlements written by the
-- updated service will populate the column.
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE bittu_settlements
    ADD COLUMN IF NOT EXISTS razorpay_cut_amount NUMERIC(14,6) NOT NULL DEFAULT 0
        CHECK (razorpay_cut_amount >= 0);

COMMENT ON COLUMN bittu_settlements.razorpay_cut_amount IS
    'Razorpay server-side cut (0.99 % + 18 % GST = 1.1682 % of gross). '
    'Intercepted by the gateway before settlement; not Bittu revenue.';

ALTER TABLE bittu_settlement_transactions
    ADD COLUMN IF NOT EXISTS razorpay_cut_amount NUMERIC(14,6) NOT NULL DEFAULT 0
        CHECK (razorpay_cut_amount >= 0);

COMMENT ON COLUMN bittu_settlement_transactions.razorpay_cut_amount IS
    'Per-transaction Razorpay cut. Sums up to the batch-level value on '
    'bittu_settlements.razorpay_cut_amount.';
