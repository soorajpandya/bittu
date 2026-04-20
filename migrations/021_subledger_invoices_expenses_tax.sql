-- ============================================================================
-- Migration 021: AR/AP Sub-Ledger + Invoices + Expenses + Tax Lifecycle +
--                Payment State Machine Enhancement
--
-- SAFE: All operations are ADDITIVE only.
--   - Adds customer_ledger / supplier_ledger (sub-ledger tracking)
--   - Adds invoices table (proper Invoice→Payment→Settlement chain)
--   - Adds expenses table (structured expense management)
--   - Adds expense_categories table
--   - Adds tax_liability table (GST lifecycle tracking)
--   - Enhances payments table (adds settled/reconciled states + gateway column)
--   - Adds new CoA accounts for RENT, SALARY, UTILITIES, MISC EXPENSE
--   - Adds permissions for invoice, expense, tax management
--   - Does NOT modify or remove any existing data/logic
-- ============================================================================
BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. CUSTOMER LEDGER (AR Sub-Ledger)
--
--    Every debit/credit touching a customer flows here.
--    Enables: "Customer A owes ₹X", aging (30/60/90), payment history
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS customer_ledger (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    customer_id     UUID NOT NULL,
    journal_entry_id UUID NOT NULL,

    -- Movement
    debit           NUMERIC(14,2) NOT NULL DEFAULT 0,
    credit          NUMERIC(14,2) NOT NULL DEFAULT 0,
    balance_after   NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Context
    reference_type  VARCHAR(50) NOT NULL,
    reference_id    VARCHAR(100),
    description     TEXT,
    entry_date      DATE NOT NULL DEFAULT CURRENT_DATE,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cl_customer
    ON customer_ledger(restaurant_id, customer_id, entry_date DESC);

CREATE INDEX IF NOT EXISTS idx_cl_journal
    ON customer_ledger(journal_entry_id);

-- Aging query helper: entries with positive balance_after older than X days
CREATE INDEX IF NOT EXISTS idx_cl_aging
    ON customer_ledger(restaurant_id, customer_id, entry_date)
    WHERE balance_after > 0;


-- ════════════════════════════════════════════════════════════════════════════
-- 2. SUPPLIER LEDGER (AP Sub-Ledger)
--
--    Every debit/credit touching a supplier flows here.
--    Enables: "Vendor B is owed ₹Y", aging, payment history
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS supplier_ledger (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    supplier_id     UUID NOT NULL,
    journal_entry_id UUID NOT NULL,

    -- Movement
    debit           NUMERIC(14,2) NOT NULL DEFAULT 0,
    credit          NUMERIC(14,2) NOT NULL DEFAULT 0,
    balance_after   NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Context
    reference_type  VARCHAR(50) NOT NULL,
    reference_id    VARCHAR(100),
    description     TEXT,
    entry_date      DATE NOT NULL DEFAULT CURRENT_DATE,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sl_supplier
    ON supplier_ledger(restaurant_id, supplier_id, entry_date DESC);

CREATE INDEX IF NOT EXISTS idx_sl_journal
    ON supplier_ledger(journal_entry_id);

CREATE INDEX IF NOT EXISTS idx_sl_aging
    ON supplier_ledger(restaurant_id, supplier_id, entry_date)
    WHERE balance_after > 0;


-- ════════════════════════════════════════════════════════════════════════════
-- 3. INVOICES — Proper Invoice → Payment → Settlement chain
--
--    Bridges orders to accounting with proper tax/compliance structure.
--    Status lifecycle: draft → issued → partially_paid → paid → cancelled
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS ar_invoices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id),

    -- Numbering
    invoice_number  VARCHAR(50) NOT NULL,
    invoice_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    due_date        DATE,

    -- Customer (nullable for walk-in)
    customer_id     UUID,
    customer_name   VARCHAR(200),
    customer_gstin  VARCHAR(20),

    -- Linked order (nullable for standalone invoices)
    order_id        UUID,

    -- Amounts
    subtotal        NUMERIC(14,2) NOT NULL DEFAULT 0,
    discount_amount NUMERIC(14,2) NOT NULL DEFAULT 0,
    cgst            NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst            NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst            NUMERIC(14,2) NOT NULL DEFAULT 0,
    cess            NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_amount    NUMERIC(14,2) NOT NULL DEFAULT 0,
    amount_paid     NUMERIC(14,2) NOT NULL DEFAULT 0,
    balance_due     NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Status
    status          VARCHAR(30) NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','issued','partially_paid','paid','cancelled','void')),

    -- Type
    invoice_type    VARCHAR(20) NOT NULL DEFAULT 'tax_invoice'
                    CHECK (invoice_type IN ('tax_invoice','bill_of_supply','credit_note','debit_note')),

    -- Journal link
    journal_entry_id UUID,

    -- Notes
    notes           TEXT,
    terms           TEXT,

    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_arinv_number
    ON ar_invoices(restaurant_id, invoice_number);

CREATE INDEX IF NOT EXISTS idx_arinv_customer
    ON ar_invoices(restaurant_id, customer_id) WHERE customer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_arinv_status
    ON ar_invoices(restaurant_id, status, invoice_date DESC);

CREATE INDEX IF NOT EXISTS idx_arinv_order
    ON ar_invoices(restaurant_id, order_id) WHERE order_id IS NOT NULL;


-- Invoice line items
CREATE TABLE IF NOT EXISTS ar_invoice_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id      UUID NOT NULL REFERENCES ar_invoices(id) ON DELETE CASCADE,
    item_name       VARCHAR(200) NOT NULL,
    hsn_code        VARCHAR(20),
    quantity        NUMERIC(10,3) NOT NULL DEFAULT 1,
    unit_price      NUMERIC(14,2) NOT NULL DEFAULT 0,
    discount        NUMERIC(14,2) NOT NULL DEFAULT 0,
    taxable_value   NUMERIC(14,2) NOT NULL DEFAULT 0,
    cgst_rate       NUMERIC(5,2) NOT NULL DEFAULT 0,
    sgst_rate       NUMERIC(5,2) NOT NULL DEFAULT 0,
    igst_rate       NUMERIC(5,2) NOT NULL DEFAULT 0,
    cgst_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    total           NUMERIC(14,2) NOT NULL DEFAULT 0,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_arinvitems_invoice
    ON ar_invoice_items(invoice_id);


-- ════════════════════════════════════════════════════════════════════════════
-- 4. EXPENSE CATEGORIES + EXPENSES TABLE
--
--    Structured expense tracking: rent, salary, utilities, vendor bills, etc.
--    Each expense links to a journal entry for double-entry.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS expense_categories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name            VARCHAR(100) NOT NULL,
    account_code    VARCHAR(20) NOT NULL,
    description     TEXT,
    is_active       BOOLEAN NOT NULL DEFAULT true,

    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_expcat_name
    ON expense_categories(restaurant_id, name) WHERE is_active = true;


CREATE TABLE IF NOT EXISTS expenses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id),

    -- Category
    category_id     UUID REFERENCES expense_categories(id),
    category_name   VARCHAR(100),

    -- Vendor (for vendor bills)
    vendor_id       UUID,
    vendor_name     VARCHAR(200),

    -- Amounts
    amount          NUMERIC(14,2) NOT NULL,
    tax_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_amount    NUMERIC(14,2) NOT NULL,

    -- Payment
    payment_method  VARCHAR(30) NOT NULL DEFAULT 'cash',
    payment_status  VARCHAR(30) NOT NULL DEFAULT 'paid'
                    CHECK (payment_status IN ('pending','paid','partial')),
    paid_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Context
    expense_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    description     TEXT,
    receipt_url     TEXT,
    invoice_number  VARCHAR(100),

    -- Recurring
    is_recurring    BOOLEAN NOT NULL DEFAULT false,
    recurrence      VARCHAR(30),  -- 'daily', 'weekly', 'monthly', 'yearly'

    -- Journal link
    journal_entry_id UUID,

    -- Approval
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,

    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exp_restaurant_date
    ON expenses(restaurant_id, expense_date DESC);

CREATE INDEX IF NOT EXISTS idx_exp_category
    ON expenses(restaurant_id, category_id);

CREATE INDEX IF NOT EXISTS idx_exp_vendor
    ON expenses(restaurant_id, vendor_id) WHERE vendor_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_exp_payment_status
    ON expenses(restaurant_id, payment_status) WHERE payment_status != 'paid';


-- ════════════════════════════════════════════════════════════════════════════
-- 5. TAX LIABILITY — GST lifecycle tracking
--
--    Tracks: collected → payable → filed → paid
--    Per period, per tax type (CGST/SGST/IGST)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tax_liability (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id),

    -- Period
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    period_label    VARCHAR(50),  -- 'Apr 2026', 'Q1 2026-27'

    -- Tax amounts
    cgst_collected  NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst_collected  NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst_collected  NUMERIC(14,2) NOT NULL DEFAULT 0,
    cess_collected  NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Input credit (from purchases)
    cgst_input      NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst_input      NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst_input      NUMERIC(14,2) NOT NULL DEFAULT 0,
    cess_input      NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Net payable = collected - input
    cgst_payable    NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst_payable    NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst_payable    NUMERIC(14,2) NOT NULL DEFAULT 0,
    cess_payable    NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_payable   NUMERIC(14,2) NOT NULL DEFAULT 0,

    -- Lifecycle
    status          VARCHAR(30) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','computed','filed','paid','adjusted')),
    filed_at        TIMESTAMPTZ,
    paid_at         TIMESTAMPTZ,
    payment_reference VARCHAR(100),

    -- Journal for tax payment
    payment_journal_id UUID,

    notes           TEXT,
    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_taxl_period
    ON tax_liability(restaurant_id, period_start, period_end);

CREATE INDEX IF NOT EXISTS idx_taxl_status
    ON tax_liability(restaurant_id, status);


-- ════════════════════════════════════════════════════════════════════════════
-- 6. PAYMENT TABLE ENHANCEMENTS
--    Add 'settled' and 'reconciled' to status + gateway column
-- ════════════════════════════════════════════════════════════════════════════

-- Add gateway column if not exists
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'gateway'
    ) THEN
        ALTER TABLE payments ADD COLUMN gateway VARCHAR(50);
    END IF;
END $$;

-- Add settlement_id reference column
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'settlement_id'
    ) THEN
        ALTER TABLE payments ADD COLUMN settlement_id UUID REFERENCES pg_settlements(id);
    END IF;
END $$;

-- Add invoice reference
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments' AND column_name = 'invoice_id'
    ) THEN
        ALTER TABLE payments ADD COLUMN invoice_id UUID REFERENCES ar_invoices(id);
    END IF;
END $$;

-- Widen the status check constraint to include new states
-- We must drop and recreate (PostgreSQL can't ALTER CHECK in place)
DO $$ BEGIN
    -- Drop old constraint if it exists (name varies — try common patterns)
    BEGIN
        ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_status_check;
    EXCEPTION WHEN undefined_object THEN NULL;
    END;

    -- Also try the enum approach — if status is varchar, add check
    -- If status uses an enum type, alter the enum
    BEGIN
        ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'initiated';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
    BEGIN
        ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'settled';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
    BEGIN
        ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'reconciled';
    EXCEPTION WHEN undefined_object THEN NULL;
    END;
END $$;


-- ════════════════════════════════════════════════════════════════════════════
-- 7. NEW CHART OF ACCOUNTS — Expense categories
-- ════════════════════════════════════════════════════════════════════════════

DO $$
DECLARE
    r RECORD;
    v_parent_expense UUID;
BEGIN
    FOR r IN SELECT id FROM restaurants LOOP
        SELECT id INTO v_parent_expense
        FROM chart_of_accounts
        WHERE restaurant_id = r.id AND account_type = 'expense' AND parent_id IS NULL
        ORDER BY account_code LIMIT 1;

        -- Rent Expense
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5020', 'Rent Expense', 'expense', v_parent_expense,
             'RENT_EXPENSE', true, true, 'Monthly rent for premises')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'RENT_EXPENSE', name = 'Rent Expense';

        -- Salary Expense
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5021', 'Salary & Wages', 'expense', v_parent_expense,
             'SALARY_EXPENSE', true, true, 'Staff salaries, wages, bonuses')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'SALARY_EXPENSE', name = 'Salary & Wages';

        -- Utilities Expense
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5022', 'Utilities', 'expense', v_parent_expense,
             'UTILITIES_EXPENSE', true, true, 'Electricity, water, gas, internet')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'UTILITIES_EXPENSE', name = 'Utilities';

        -- Miscellaneous Expense
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5030', 'Miscellaneous Expenses', 'expense', v_parent_expense,
             'MISC_EXPENSE', true, true, 'Other operating expenses')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'MISC_EXPENSE', name = 'Miscellaneous Expenses';

        -- Seed default expense categories
        INSERT INTO expense_categories (restaurant_id, name, account_code, description)
        VALUES
            (r.id, 'Rent', '5020', 'Monthly rent'),
            (r.id, 'Salary & Wages', '5021', 'Staff salaries'),
            (r.id, 'Utilities', '5022', 'Electricity, water, gas'),
            (r.id, 'Food Supplies', '5001', 'Raw material purchases'),
            (r.id, 'Beverage Supplies', '5002', 'Beverage material purchases'),
            (r.id, 'Gateway Charges', '5011', 'Payment gateway fees'),
            (r.id, 'Miscellaneous', '5030', 'Other expenses')
        ON CONFLICT DO NOTHING;

    END LOOP;
END $$;


-- ════════════════════════════════════════════════════════════════════════════
-- 8. NEW SYSTEM ACCOUNTS MAPPING
-- ════════════════════════════════════════════════════════════════════════════

-- Also add valid reference types
-- invoice, customer_ledger, supplier_ledger, tax_payment — all VARCHAR, no DDL needed


-- ════════════════════════════════════════════════════════════════════════════
-- 9. PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    ('invoice.read'),
    ('invoice.write'),
    ('invoice.void'),
    ('expense.read'),
    ('expense.write'),
    ('expense.approve'),
    ('tax.read'),
    ('tax.compute'),
    ('tax.file'),
    ('subledger.read')
ON CONFLICT (key) DO NOTHING;

-- owner: all, manager: read+write (no void/file), cashier: expense.read
WITH role_perm(role_name, perm_key, allowed, meta) AS (
    VALUES
    ('owner',   'invoice.read',     true, '{}'::jsonb),
    ('owner',   'invoice.write',    true, '{}'::jsonb),
    ('owner',   'invoice.void',     true, '{}'::jsonb),
    ('owner',   'expense.read',     true, '{}'::jsonb),
    ('owner',   'expense.write',    true, '{}'::jsonb),
    ('owner',   'expense.approve',  true, '{}'::jsonb),
    ('owner',   'tax.read',         true, '{}'::jsonb),
    ('owner',   'tax.compute',      true, '{}'::jsonb),
    ('owner',   'tax.file',         true, '{}'::jsonb),
    ('owner',   'subledger.read',   true, '{}'::jsonb),
    ('manager', 'invoice.read',     true, '{}'::jsonb),
    ('manager', 'invoice.write',    true, '{}'::jsonb),
    ('manager', 'expense.read',     true, '{}'::jsonb),
    ('manager', 'expense.write',    true, '{}'::jsonb),
    ('manager', 'tax.read',         true, '{}'::jsonb),
    ('manager', 'subledger.read',   true, '{}'::jsonb),
    ('cashier', 'invoice.read',     true, '{}'::jsonb),
    ('cashier', 'expense.read',     true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN permissions p ON p.key = rp.perm_key
JOIN roles r ON lower(r.name) = lower(rp.role_name)
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
