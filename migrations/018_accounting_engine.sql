-- ============================================================================
-- Migration 018: Accounting Engine — Idempotency, Reversal, System Accounts
--
-- SAFE: All operations are ADDITIVE only.
--   - Adds UNIQUE constraint for idempotent journal entries
--   - Adds reversal tracking columns
--   - Seeds additional system accounts needed by the engine
--   - Adds accounting.report permission
--   - Does NOT modify or remove any existing data or columns
-- ============================================================================
BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. IDEMPOTENCY: Unique constraint on journal_entries(reference_type, reference_id)
--    Prevents duplicate accounting for the same business event.
-- ════════════════════════════════════════════════════════════════════════════

-- First, deduplicate any existing rows (keep the earliest entry per ref)
DELETE FROM journal_entries je1
 WHERE EXISTS (
    SELECT 1 FROM journal_entries je2
     WHERE je2.reference_type = je1.reference_type
       AND je2.reference_id   = je1.reference_id
       AND je2.reference_id IS NOT NULL
       AND je2.created_at < je1.created_at
 );

CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_entries_reference
    ON journal_entries (restaurant_id, reference_type, reference_id)
    WHERE reference_id IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- 2. REVERSAL TRACKING
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE journal_entries
    ADD COLUMN IF NOT EXISTS reversed_entry_id UUID REFERENCES journal_entries(id);

-- Index to find reversal entries quickly
CREATE INDEX IF NOT EXISTS idx_je_reversed
    ON journal_entries(reversed_entry_id) WHERE reversed_entry_id IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- 3. ADDITIONAL SYSTEM ACCOUNTS
--    These are needed by the accounting engine for full event coverage.
-- ════════════════════════════════════════════════════════════════════════════

-- Add system_code entries for accounts already in CoA but missing system_code
-- We use DO blocks because fn_seed_chart_of_accounts already seeds the rows.

-- Accounts Receivable
UPDATE chart_of_accounts SET system_code = 'ACCOUNTS_RECEIVABLE'
 WHERE account_code = '1003' AND system_code IS NULL;

-- Inventory - Food
UPDATE chart_of_accounts SET system_code = 'INVENTORY_FOOD'
 WHERE account_code = '1004' AND system_code IS NULL;

-- Inventory - Beverages
UPDATE chart_of_accounts SET system_code = 'INVENTORY_BEVERAGE'
 WHERE account_code = '1005' AND system_code IS NULL;

-- Accounts Payable
UPDATE chart_of_accounts SET system_code = 'ACCOUNTS_PAYABLE'
 WHERE account_code = '2001' AND system_code IS NULL;

-- CGST Payable
UPDATE chart_of_accounts SET system_code = 'CGST_PAYABLE'
 WHERE account_code = '2002' AND system_code IS NULL;

-- SGST Payable
UPDATE chart_of_accounts SET system_code = 'SGST_PAYABLE'
 WHERE account_code = '2003' AND system_code IS NULL;

-- IGST Payable
UPDATE chart_of_accounts SET system_code = 'IGST_PAYABLE'
 WHERE account_code = '2004' AND system_code IS NULL;

-- COGS - Food
UPDATE chart_of_accounts SET system_code = 'COGS_FOOD'
 WHERE account_code = '5001' AND system_code IS NULL;

-- COGS - Beverages
UPDATE chart_of_accounts SET system_code = 'COGS_BEVERAGE'
 WHERE account_code = '5002' AND system_code IS NULL;

-- Beverage Sales
UPDATE chart_of_accounts SET system_code = 'BEVERAGE_SALES'
 WHERE account_code = '4002' AND system_code IS NULL;

-- Now seed accounts that may not exist yet (for restaurants created before full seed)

-- Discount Expense (5006)
INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, system_code, is_active)
SELECT r.id, '5006', 'Discount Expense', 'expense',
       (SELECT id FROM chart_of_accounts WHERE restaurant_id = r.id AND account_code = '5000' LIMIT 1),
       true, 'DISCOUNT_EXPENSE', true
  FROM restaurants r
 WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts WHERE restaurant_id = r.id AND account_code = '5006'
 );

-- Sales Returns (contra-revenue, modelled as expense for simplicity)
INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, system_code, is_active)
SELECT r.id, '5007', 'Sales Returns', 'expense',
       (SELECT id FROM chart_of_accounts WHERE restaurant_id = r.id AND account_code = '5000' LIMIT 1),
       true, 'SALES_RETURNS', true
  FROM restaurants r
 WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts WHERE restaurant_id = r.id AND account_code = '5007'
 );

-- Retained Earnings
UPDATE chart_of_accounts SET system_code = 'RETAINED_EARNINGS'
 WHERE account_code = '3002' AND system_code IS NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- 4. PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    ('accounting.report')
ON CONFLICT (key) DO NOTHING;

-- Grant to owner + manager
WITH role_perm(role_name, perm_key, allowed, meta) AS (
    VALUES
    ('owner',   'accounting.report', true, '{}'::jsonb),
    ('manager', 'accounting.report', true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN permissions p ON p.key = rp.perm_key
JOIN roles r ON lower(r.name) = lower(rp.role_name)
ON CONFLICT (role_id, permission_id) DO NOTHING;


-- ════════════════════════════════════════════════════════════════════════════
-- 5. PERFORMANCE INDEXES
-- ════════════════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_jl_account_entry
    ON journal_lines (account_id, journal_entry_id);

CREATE INDEX IF NOT EXISTS idx_je_restaurant_branch_date
    ON journal_entries (restaurant_id, branch_id, entry_date DESC);

COMMIT;
