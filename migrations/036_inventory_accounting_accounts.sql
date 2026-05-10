-- ════════════════════════════════════════════════════════════════════════════
-- Migration 036 — Inventory Accounting Accounts
-- Section 5: Accounting Integration
-- Seeds WASTAGE_EXPENSE in chart_of_accounts for every restaurant so the
-- inventory event handlers can post DR Wastage / CR Inventory journals.
-- Idempotent — uses ON CONFLICT.
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

DO $$
DECLARE
    r RECORD;
    v_parent_expense UUID;
BEGIN
    FOR r IN SELECT id FROM restaurants LOOP
        SELECT id INTO v_parent_expense
          FROM chart_of_accounts
         WHERE restaurant_id = r.id
           AND account_type  = 'expense'
           AND parent_id IS NULL
         LIMIT 1;

        -- Wastage Expense (5040)
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5040', 'Inventory Wastage', 'expense', v_parent_expense,
             'WASTAGE_EXPENSE', true, true,
             'Inventory wastage (spoilage, breakage, expiry)')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'WASTAGE_EXPENSE',
                name        = 'Inventory Wastage';
    END LOOP;
END $$;

COMMIT;
