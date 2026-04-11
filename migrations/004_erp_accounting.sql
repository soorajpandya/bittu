-- Migration 004: ERP Layer — accounting_entries table
-- Run in Supabase SQL Editor

-- ── Accounting Entries ──
CREATE TABLE IF NOT EXISTS accounting_entries (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id         TEXT NOT NULL,
    restaurant_id   TEXT,
    branch_id       UUID,
    entry_type      VARCHAR(20) NOT NULL CHECK (entry_type IN ('revenue', 'expense', 'refund')),
    amount          NUMERIC(12,2) NOT NULL,  -- positive for revenue, negative for expense/refund
    payment_method  VARCHAR(30),
    category        VARCHAR(100),
    reference_type  VARCHAR(50),              -- 'order', 'purchase_order', 'manual', etc.
    reference_id    TEXT,
    description     TEXT DEFAULT '',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_acct_entries_user_date
    ON accounting_entries (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_acct_entries_branch_date
    ON accounting_entries (branch_id, created_at DESC)
    WHERE branch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_acct_entries_type
    ON accounting_entries (user_id, entry_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_acct_entries_ref
    ON accounting_entries (reference_type, reference_id)
    WHERE reference_id IS NOT NULL;
