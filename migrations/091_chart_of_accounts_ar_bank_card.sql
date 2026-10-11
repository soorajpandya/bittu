-- =====================================================================
-- Migration 091 — Chart of accounts: distinct Accounts Receivable, Bank and
--                 Card accounts; reclass misposted history
-- =====================================================================
-- Problem: accounting_engine mapped both ACCOUNTS_RECEIVABLE and CARD to
-- account_code 1003, and 011 relabelled 1003 as CARD_ACCOUNT so 018's AR
-- system_code update was a no-op. Every receivable line has been landing in
-- the Card account. BANK mapped to UPI_ACCOUNT (1002), and 020 converted
-- 1006 'Prepaid Expenses' into PG Clearing.
--
-- Fix (idempotent, per restaurant):
--   1007 Accounts Receivable  system_code ACCOUNTS_RECEIVABLE  (new)
--   1008 Bank Account         system_code BANK_ACCOUNT         (new)
--   1009 Prepaid Expenses     (recreated only if pre-020 lines used 1006)
--   1003 Card Receipts        system_code CARD_ACCOUNT         (restored)
--   1002 UPI Account          system_code UPI_ACCOUNT          (unchanged)
-- Historical journal_lines are immutable (019/023/050), so misposted history
-- is corrected with balancing reclass journals: dated on the original
-- entry_date when that period is open, dated CURRENT_DATE when it is
-- closed/locked (reference_type 'reversal',
-- reference_id coa_reclass_091:<kind>:<date>:<branch>).
-- The seed function is replaced so new restaurants get the same layout.
-- =====================================================================
BEGIN;

-- ── 1. Seed function (new restaurants) ────────────────────────────────
CREATE OR REPLACE FUNCTION fn_seed_chart_of_accounts(p_restaurant_id UUID)
RETURNS void AS $$
DECLARE
    v_asset_id      UUID;
    v_liability_id  UUID;
    v_equity_id     UUID;
    v_revenue_id    UUID;
    v_expense_id    UUID;
BEGIN
    IF EXISTS (SELECT 1 FROM chart_of_accounts WHERE restaurant_id = p_restaurant_id LIMIT 1) THEN
        RETURN;
    END IF;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '1000', 'Assets', 'asset', true)
    RETURNING id INTO v_asset_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, system_code) VALUES
        (p_restaurant_id, '1001', 'Cash',                      'asset', v_asset_id, true,  'CASH_ACCOUNT'),
        (p_restaurant_id, '1002', 'UPI Account',               'asset', v_asset_id, true,  'UPI_ACCOUNT'),
        (p_restaurant_id, '1003', 'Card Receipts',             'asset', v_asset_id, true,  'CARD_ACCOUNT'),
        (p_restaurant_id, '1004', 'Inventory - Food',          'asset', v_asset_id, true,  'INVENTORY_FOOD'),
        (p_restaurant_id, '1005', 'Inventory - Beverages',     'asset', v_asset_id, true,  'INVENTORY_BEVERAGE'),
        (p_restaurant_id, '1006', 'Payment Gateway Clearing',  'asset', v_asset_id, true,  'PG_CLEARING'),
        (p_restaurant_id, '1007', 'Accounts Receivable',       'asset', v_asset_id, true,  'ACCOUNTS_RECEIVABLE'),
        (p_restaurant_id, '1008', 'Bank Account',              'asset', v_asset_id, true,  'BANK_ACCOUNT'),
        (p_restaurant_id, '1009', 'Prepaid Expenses',          'asset', v_asset_id, false, NULL);

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '2000', 'Liabilities', 'liability', true)
    RETURNING id INTO v_liability_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, system_code) VALUES
        (p_restaurant_id, '2001', 'Accounts Payable',   'liability', v_liability_id, true,  'ACCOUNTS_PAYABLE'),
        (p_restaurant_id, '2002', 'CGST Payable',       'liability', v_liability_id, true,  'CGST_PAYABLE'),
        (p_restaurant_id, '2003', 'SGST Payable',       'liability', v_liability_id, true,  'SGST_PAYABLE'),
        (p_restaurant_id, '2004', 'IGST Payable',       'liability', v_liability_id, true,  'IGST_PAYABLE'),
        (p_restaurant_id, '2005', 'Salary Payable',     'liability', v_liability_id, false, NULL),
        (p_restaurant_id, '2006', 'Other Payables',     'liability', v_liability_id, false, NULL);

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '3000', 'Equity', 'equity', true)
    RETURNING id INTO v_equity_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system) VALUES
        (p_restaurant_id, '3001', 'Owner Capital',      'equity', v_equity_id, true),
        (p_restaurant_id, '3002', 'Retained Earnings',  'equity', v_equity_id, true);

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '4000', 'Revenue', 'revenue', true)
    RETURNING id INTO v_revenue_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, system_code) VALUES
        (p_restaurant_id, '4001', 'Food Sales',        'revenue', v_revenue_id, true,  'FOOD_SALES'),
        (p_restaurant_id, '4002', 'Beverage Sales',    'revenue', v_revenue_id, true,  'BEVERAGE_SALES'),
        (p_restaurant_id, '4003', 'Delivery Income',   'revenue', v_revenue_id, false, NULL),
        (p_restaurant_id, '4004', 'Service Charges',   'revenue', v_revenue_id, false, NULL),
        (p_restaurant_id, '4005', 'Other Income',      'revenue', v_revenue_id, false, NULL);

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '5000', 'Expenses', 'expense', true)
    RETURNING id INTO v_expense_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, system_code) VALUES
        (p_restaurant_id, '5001', 'COGS - Food',               'expense', v_expense_id, true,  'COGS_FOOD'),
        (p_restaurant_id, '5002', 'COGS - Beverages',          'expense', v_expense_id, true,  'COGS_BEVERAGE'),
        (p_restaurant_id, '5003', 'Staff Salaries',            'expense', v_expense_id, false, 'SALARY_EXPENSE'),
        (p_restaurant_id, '5004', 'Rent',                      'expense', v_expense_id, false, 'RENT_EXPENSE'),
        (p_restaurant_id, '5005', 'Utilities',                 'expense', v_expense_id, false, 'UTILITIES_EXPENSE'),
        (p_restaurant_id, '5006', 'Discounts Given',           'expense', v_expense_id, true,  'DISCOUNT_EXPENSE'),
        (p_restaurant_id, '5007', 'Sales Returns',             'expense', v_expense_id, true,  'SALES_RETURNS'),
        (p_restaurant_id, '5008', 'Delivery Charges',          'expense', v_expense_id, false, NULL),
        (p_restaurant_id, '5009', 'Platform Commissions',      'expense', v_expense_id, false, NULL),
        (p_restaurant_id, '5010', 'Packaging',                 'expense', v_expense_id, false, NULL),
        (p_restaurant_id, '5011', 'Payment Gateway Charges',   'expense', v_expense_id, true,  'GATEWAY_CHARGES'),
        (p_restaurant_id, '5012', 'GST on Gateway Charges',    'expense', v_expense_id, true,  'GATEWAY_TAX'),
        (p_restaurant_id, '5020', 'Rent Expense',              'expense', v_expense_id, false, NULL),
        (p_restaurant_id, '5021', 'Salary Expense',            'expense', v_expense_id, false, NULL),
        (p_restaurant_id, '5022', 'Utilities Expense',         'expense', v_expense_id, false, NULL),
        (p_restaurant_id, '5030', 'Miscellaneous Expense',     'expense', v_expense_id, false, 'MISC_EXPENSE'),
        (p_restaurant_id, '5040', 'Wastage',                   'expense', v_expense_id, false, 'WASTAGE_EXPENSE');
END;
$$ LANGUAGE plpgsql;

-- ── 2. Existing restaurants: create/restore accounts and reclass ──────
DO $$
DECLARE
    r               RECORD;
    v_parent_asset  UUID;
    v_card_id       UUID;      -- 1003
    v_ar_id         UUID;      -- 1007
    v_bank_id       UUID;      -- 1008
    v_upi_id        UUID;      -- 1002
    v_pg_id         UUID;      -- 1006
    v_prepaid_id    UUID;      -- 1009
    v_misposted     RECORD;
    v_je_id         UUID;
BEGIN
    FOR r IN SELECT id FROM restaurants LOOP
        SELECT id INTO v_parent_asset FROM chart_of_accounts
         WHERE restaurant_id = r.id AND account_type = 'asset' AND parent_id IS NULL
         ORDER BY account_code LIMIT 1;

        -- 1003 → Card Receipts
        INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, system_code, is_system, is_active)
        VALUES (r.id, '1003', 'Card Receipts', 'asset', v_parent_asset, 'CARD_ACCOUNT', true, true)
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'CARD_ACCOUNT', name = 'Card Receipts', is_system = true, is_active = true
        RETURNING id INTO v_card_id;

        -- 1007 Accounts Receivable
        INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, system_code, is_system, is_active, description)
        VALUES (r.id, '1007', 'Accounts Receivable', 'asset', v_parent_asset, 'ACCOUNTS_RECEIVABLE', true, true,
                'Amounts owed by customers for orders not yet paid')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'ACCOUNTS_RECEIVABLE', name = 'Accounts Receivable', is_system = true, is_active = true
        RETURNING id INTO v_ar_id;

        -- 1008 Bank Account
        INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, system_code, is_system, is_active, description)
        VALUES (r.id, '1008', 'Bank Account', 'asset', v_parent_asset, 'BANK_ACCOUNT', true, true,
                'Settlements and deposits received in the bank')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'BANK_ACCOUNT', name = 'Bank Account', is_system = true, is_active = true
        RETURNING id INTO v_bank_id;

        -- Any stray ACCOUNTS_RECEIVABLE/BANK_ACCOUNT system_code on another code must yield.
        UPDATE chart_of_accounts SET system_code = NULL
         WHERE restaurant_id = r.id AND system_code = 'ACCOUNTS_RECEIVABLE' AND account_code <> '1007';
        UPDATE chart_of_accounts SET system_code = NULL
         WHERE restaurant_id = r.id AND system_code = 'BANK_ACCOUNT' AND account_code <> '1008';

        SELECT id INTO v_upi_id FROM chart_of_accounts WHERE restaurant_id = r.id AND account_code = '1002';
        SELECT id INTO v_pg_id  FROM chart_of_accounts WHERE restaurant_id = r.id AND account_code = '1006';

        -- 1009 Prepaid Expenses only if pre-020 lines used 1006 for non-gateway entries.
        IF v_pg_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM journal_lines jl JOIN journal_entries je ON je.id = jl.journal_entry_id
            WHERE jl.account_id = v_pg_id
              AND je.reference_type NOT IN ('payment','settlement','gateway_fee','refund','partial_refund','chargeback','reversal','bank_recon')
        ) THEN
            INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system, is_active)
            VALUES (r.id, '1009', 'Prepaid Expenses', 'asset', v_parent_asset, false, true)
            ON CONFLICT (restaurant_id, account_code) DO UPDATE SET is_active = true
            RETURNING id INTO v_prepaid_id;
        ELSE
            v_prepaid_id := NULL;
        END IF;

        -- ── Reclass via balancing journals (journal_lines are immutable) ──
        -- Open period  → reclass entry dated on the original entry_date.
        -- Closed/locked → reclass entry dated CURRENT_DATE.
        -- One entry per (kind, original date, branch, lock state); idempotent.
        FOR v_misposted IN
            WITH mis AS (
                -- Lines posted via logical AR that landed on Card 1003
                SELECT je.entry_date, je.branch_id, jl.debit, jl.credit, 'ar'::text AS kind,
                       v_card_id AS from_id, v_ar_id AS to_id
                FROM journal_lines jl JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.restaurant_id = r.id AND jl.account_id = v_card_id
                  AND (jl.description ILIKE '%receivable%' OR je.reference_type IN ('order','invoice','discount'))
                UNION ALL
                -- Settlement deposits posted via logical BANK that landed on UPI 1002
                SELECT je.entry_date, je.branch_id, jl.debit, jl.credit, 'bank', v_upi_id, v_bank_id
                FROM journal_lines jl JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.restaurant_id = r.id AND v_upi_id IS NOT NULL AND jl.account_id = v_upi_id
                  AND je.reference_type = 'settlement'
                UNION ALL
                -- Pre-020 prepaid expense lines on 1006
                SELECT je.entry_date, je.branch_id, jl.debit, jl.credit, 'prepaid', v_pg_id, v_prepaid_id
                FROM journal_lines jl JOIN journal_entries je ON je.id = jl.journal_entry_id
                WHERE je.restaurant_id = r.id AND v_prepaid_id IS NOT NULL AND jl.account_id = v_pg_id
                  AND je.reference_type NOT IN ('payment','settlement','gateway_fee','refund','partial_refund','chargeback','reversal','bank_recon')
            )
            SELECT kind, entry_date, branch_id, from_id, to_id,
                   SUM(debit)::numeric(14,2) AS debit, SUM(credit)::numeric(14,2) AS credit,
                   EXISTS (SELECT 1 FROM accounting_periods ap WHERE ap.restaurant_id = r.id
                             AND ap.status IN ('closed','locked') AND mis.entry_date BETWEEN ap.period_start AND ap.period_end) AS locked
            FROM mis
            WHERE to_id IS NOT NULL
              -- Skip lines that are themselves migration-091 reclass legs.
            GROUP BY kind, entry_date, branch_id, from_id, to_id
            HAVING SUM(debit) <> SUM(credit)
        LOOP
            IF EXISTS (SELECT 1 FROM journal_entries WHERE restaurant_id = r.id AND reference_type = 'reversal'
                         AND reference_id = 'coa_reclass_091:' || v_misposted.kind || ':' || v_misposted.entry_date::text
                                            || ':' || COALESCE(v_misposted.branch_id::text, 'all')) THEN
                CONTINUE;
            END IF;
            INSERT INTO journal_entries (restaurant_id, branch_id, entry_date, reference_type, reference_id, description, created_by)
            VALUES (r.id, v_misposted.branch_id,
                    CASE WHEN v_misposted.locked THEN CURRENT_DATE ELSE v_misposted.entry_date END,
                    'reversal',
                    'coa_reclass_091:' || v_misposted.kind || ':' || v_misposted.entry_date::text
                        || ':' || COALESCE(v_misposted.branch_id::text, 'all'),
                    'Reclass ' || v_misposted.kind || ' postings of ' || v_misposted.entry_date::text
                        || CASE WHEN v_misposted.locked THEN ' (closed period; dated today)' ELSE '' END,
                    'migration_091')
            RETURNING id INTO v_je_id;
            -- Net movement only: reverse on the wrong account, re-post on the right one.
            IF v_misposted.debit > v_misposted.credit THEN
                INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, description) VALUES
                    (v_je_id, v_misposted.from_id, 0, v_misposted.debit - v_misposted.credit, 'Reclass out (migration 091)'),
                    (v_je_id, v_misposted.to_id,   v_misposted.debit - v_misposted.credit, 0, 'Reclass in (migration 091)');
            ELSE
                INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, description) VALUES
                    (v_je_id, v_misposted.from_id, v_misposted.credit - v_misposted.debit, 0, 'Reclass out (migration 091)'),
                    (v_je_id, v_misposted.to_id,   0, v_misposted.credit - v_misposted.debit, 'Reclass in (migration 091)');
            END IF;
        END LOOP;
    END LOOP;
END$$;

-- ── 3. Guard: no two system codes may share an account code per restaurant ──
DO $$
DECLARE dup RECORD;
BEGIN
    FOR dup IN
        SELECT restaurant_id, system_code, COUNT(*) AS n FROM chart_of_accounts
        WHERE system_code IS NOT NULL AND is_active GROUP BY restaurant_id, system_code HAVING COUNT(*) > 1
    LOOP
        RAISE EXCEPTION 'duplicate active system_code % for restaurant %', dup.system_code, dup.restaurant_id;
    END LOOP;
END$$;

COMMIT;
