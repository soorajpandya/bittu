-- Migration 077: Soft-void support for accounting_entries
-- Enables PUT/DELETE on manual expense/income entries while preserving
-- the immutable journal_entries audit trail (a reversal journal is posted,
-- and the legacy bridge row is marked voided).
--
-- Safe to re-run.

BEGIN;

ALTER TABLE accounting_entries
    ADD COLUMN IF NOT EXISTS voided_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS voided_by           TEXT,
    ADD COLUMN IF NOT EXISTS void_reason         TEXT,
    ADD COLUMN IF NOT EXISTS reversal_journal_id UUID REFERENCES journal_entries(id);

-- Partial index for the common "show only live rows" filter.
CREATE INDEX IF NOT EXISTS idx_acct_entries_live
    ON accounting_entries (user_id, created_at DESC)
    WHERE voided_at IS NULL;

COMMIT;
