-- Migration 011: Add system_code to chart_of_accounts + account_id FK to accounting_entries
-- SAFE: Additive only. No drops, no type changes.
-- Depends on: 006_erp_full_system.sql (journal_lines), 010_double_entry_accounting_bridge.sql

-- ── 1. system_code on chart_of_accounts ──────────────────────────────────────
-- Unique identifier per restaurant used by code (FOOD_SALES, CASH_ACCOUNT, etc.)
ALTER TABLE chart_of_accounts
    ADD COLUMN IF NOT EXISTS system_code VARCHAR(50);

CREATE UNIQUE INDEX IF NOT EXISTS idx_coa_system_code
    ON chart_of_accounts(restaurant_id, system_code)
    WHERE system_code IS NOT NULL;

-- ── 2. account_id FK on accounting_entries → chart_of_accounts ──────────────
-- Links every double-entry row to its CoA account.
-- NULL allowed: legacy rows predate this column.
ALTER TABLE accounting_entries
    ADD COLUMN IF NOT EXISTS account_id UUID REFERENCES chart_of_accounts(id);

CREATE INDEX IF NOT EXISTS idx_accounting_entries_account_id
    ON accounting_entries(account_id)
    WHERE account_id IS NOT NULL;

-- ── 3. Seed system accounts for existing restaurants ─────────────────────────
-- Inserts the three required system accounts (1001, 1002, 4001) for every
-- restaurant that already has a chart_of_accounts row but not these codes yet.
-- ON CONFLICT DO NOTHING is safe to re-run.

INSERT INTO chart_of_accounts
    (restaurant_id, account_code, name, account_type, system_code, is_system, is_active)
SELECT
    r.id,
    '1001',
    'Cash Account',
    'asset',
    'CASH_ACCOUNT',
    true,
    true
FROM restaurants r
WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts c
    WHERE c.restaurant_id = r.id AND c.account_code = '1001'
)
ON CONFLICT (restaurant_id, account_code) DO UPDATE
    SET system_code = EXCLUDED.system_code,
        is_system   = true;

INSERT INTO chart_of_accounts
    (restaurant_id, account_code, name, account_type, system_code, is_system, is_active)
SELECT
    r.id,
    '1002',
    'Bank/UPI Account',
    'asset',
    'UPI_ACCOUNT',
    true,
    true
FROM restaurants r
WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts c
    WHERE c.restaurant_id = r.id AND c.account_code = '1002'
)
ON CONFLICT (restaurant_id, account_code) DO UPDATE
    SET system_code = EXCLUDED.system_code,
        is_system   = true;

INSERT INTO chart_of_accounts
    (restaurant_id, account_code, name, account_type, system_code, is_system, is_active)
SELECT
    r.id,
    '1003',
    'Card Account',
    'asset',
    'CARD_ACCOUNT',
    true,
    true
FROM restaurants r
WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts c
    WHERE c.restaurant_id = r.id AND c.account_code = '1003'
)
ON CONFLICT (restaurant_id, account_code) DO UPDATE
    SET system_code = EXCLUDED.system_code,
        is_system   = true;

INSERT INTO chart_of_accounts
    (restaurant_id, account_code, name, account_type, system_code, is_system, is_active)
SELECT
    r.id,
    '4001',
    'Food Sales',
    'revenue',
    'FOOD_SALES',
    true,
    true
FROM restaurants r
WHERE NOT EXISTS (
    SELECT 1 FROM chart_of_accounts c
    WHERE c.restaurant_id = r.id AND c.account_code = '4001'
)
ON CONFLICT (restaurant_id, account_code) DO UPDATE
    SET system_code = EXCLUDED.system_code,
        is_system   = true;

-- ── 4. Back-fill system_code on existing rows (if account_code matches) ──────
UPDATE chart_of_accounts SET system_code = 'CASH_ACCOUNT'  WHERE system_code IS NULL AND account_code = '1001';
UPDATE chart_of_accounts SET system_code = 'UPI_ACCOUNT'   WHERE system_code IS NULL AND account_code = '1002';
UPDATE chart_of_accounts SET system_code = 'CARD_ACCOUNT'  WHERE system_code IS NULL AND account_code = '1003';
UPDATE chart_of_accounts SET system_code = 'FOOD_SALES'    WHERE system_code IS NULL AND account_code = '4001';
