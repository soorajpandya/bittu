-- ============================================================================
-- MIGRATION 006: Full ERP System
-- Double-Entry Accounting, Recipes, Inventory Ledger, Vendors, GRN,
-- Cash Control, Shifts, Inter-Branch Transfers, GST, Analytics
--
-- SAFE: All operations are ADDITIVE only.
--   - New tables (CREATE TABLE IF NOT EXISTS)
--   - New columns (ALTER TABLE ADD COLUMN IF NOT EXISTS)
--   - New indexes, constraints, functions, triggers, views
--   - No existing tables or columns are modified or removed
--
-- PERFORMANCE: ERP tables are populated ASYNC via domain events.
--   Orders & payments NEVER join ERP tables in the hot path.
--
-- Run in Supabase SQL Editor AFTER 005_purchase_invoices.sql
-- ============================================================================


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 0: SEQUENCES
-- ════════════════════════════════════════════════════════════════════════════

CREATE SEQUENCE IF NOT EXISTS grn_number_seq START 1001;
CREATE SEQUENCE IF NOT EXISTS transfer_number_seq START 1001;
CREATE SEQUENCE IF NOT EXISTS gst_invoice_number_seq START 1;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 1: CHART OF ACCOUNTS (Double-Entry Engine)
-- Hierarchical account tree per restaurant.
-- Types: asset, liability, equity, revenue, expense
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS chart_of_accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    account_code    VARCHAR(20) NOT NULL,
    name            VARCHAR(255) NOT NULL,
    account_type    VARCHAR(20) NOT NULL CHECK (account_type IN (
                        'asset', 'liability', 'equity', 'revenue', 'expense'
                    )),
    parent_id       UUID REFERENCES chart_of_accounts(id) ON DELETE SET NULL,
    description     TEXT,
    is_system       BOOLEAN DEFAULT false,   -- system accounts cannot be deleted
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, account_code)
);

CREATE INDEX IF NOT EXISTS idx_coa_restaurant   ON chart_of_accounts(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_coa_parent       ON chart_of_accounts(parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_coa_type         ON chart_of_accounts(restaurant_id, account_type);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 2: JOURNAL ENTRIES + LINES (Double-Entry Transactions)
-- Every financial event creates a journal entry with balanced debit/credit.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS journal_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id),
    entry_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    reference_type  VARCHAR(50) NOT NULL,  -- order, payment, refund, purchase, expense, grn, transfer, adjustment
    reference_id    TEXT,
    description     TEXT,
    is_reversed     BOOLEAN DEFAULT false,
    reversed_by     UUID REFERENCES journal_entries(id),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_je_restaurant_date   ON journal_entries(restaurant_id, entry_date DESC);
CREATE INDEX IF NOT EXISTS idx_je_branch_date       ON journal_entries(branch_id, entry_date DESC) WHERE branch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_je_reference          ON journal_entries(reference_type, reference_id) WHERE reference_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS journal_lines (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_entry_id    UUID NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_id          UUID NOT NULL REFERENCES chart_of_accounts(id),
    debit               NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit              NUMERIC(14,2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    description         TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    CHECK (debit > 0 OR credit > 0)  -- every line must move money
);

CREATE INDEX IF NOT EXISTS idx_jl_entry     ON journal_lines(journal_entry_id);
CREATE INDEX IF NOT EXISTS idx_jl_account   ON journal_lines(account_id);

-- ── Trigger: Validate debit = credit at COMMIT time ──
CREATE OR REPLACE FUNCTION fn_validate_journal_balance()
RETURNS TRIGGER AS $$
DECLARE
    v_total_debit   NUMERIC(14,2);
    v_total_credit  NUMERIC(14,2);
BEGIN
    SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0)
      INTO v_total_debit, v_total_credit
      FROM journal_lines
     WHERE journal_entry_id = NEW.journal_entry_id;

    IF v_total_debit != v_total_credit THEN
        RAISE EXCEPTION 'Journal entry % is unbalanced: debit=%, credit=%',
            NEW.journal_entry_id, v_total_debit, v_total_credit;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Deferred: fires at COMMIT after all lines are inserted
DROP TRIGGER IF EXISTS trg_validate_journal_balance ON journal_lines;
CREATE CONSTRAINT TRIGGER trg_validate_journal_balance
    AFTER INSERT OR UPDATE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION fn_validate_journal_balance();

-- ── Helper function: create a balanced journal entry in one call ──
CREATE OR REPLACE FUNCTION fn_create_journal_entry(
    p_restaurant_id UUID,
    p_branch_id     UUID,
    p_entry_date    DATE,
    p_ref_type      VARCHAR,
    p_ref_id        TEXT,
    p_description   TEXT,
    p_created_by    TEXT,
    p_lines         JSONB   -- [{account_id, debit, credit, description}]
) RETURNS UUID AS $$
DECLARE
    v_entry_id      UUID;
    v_total_debit   NUMERIC(14,2) := 0;
    v_total_credit  NUMERIC(14,2) := 0;
    v_line          JSONB;
BEGIN
    -- Pre-validate balance
    FOR v_line IN SELECT * FROM jsonb_array_elements(p_lines)
    LOOP
        v_total_debit  := v_total_debit  + COALESCE((v_line->>'debit')::NUMERIC, 0);
        v_total_credit := v_total_credit + COALESCE((v_line->>'credit')::NUMERIC, 0);
    END LOOP;

    IF v_total_debit != v_total_credit THEN
        RAISE EXCEPTION 'Unbalanced journal entry: debit=%, credit=%', v_total_debit, v_total_credit;
    END IF;

    -- Insert header
    INSERT INTO journal_entries
        (restaurant_id, branch_id, entry_date, reference_type, reference_id, description, created_by)
    VALUES
        (p_restaurant_id, p_branch_id, p_entry_date, p_ref_type, p_ref_id, p_description, p_created_by)
    RETURNING id INTO v_entry_id;

    -- Insert lines
    FOR v_line IN SELECT * FROM jsonb_array_elements(p_lines)
    LOOP
        INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, description)
        VALUES (
            v_entry_id,
            (v_line->>'account_id')::UUID,
            COALESCE((v_line->>'debit')::NUMERIC, 0),
            COALESCE((v_line->>'credit')::NUMERIC, 0),
            v_line->>'description'
        );
    END LOOP;

    RETURN v_entry_id;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 3: RECIPES (Formalized item → ingredient mappings)
-- Separate from existing item_ingredients for backward compat.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS recipes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    item_id         INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
    name            VARCHAR(255),
    yield_quantity  NUMERIC(10,3) NOT NULL DEFAULT 1,   -- portions per batch
    yield_unit      VARCHAR(50) DEFAULT 'portion',
    is_active       BOOLEAN DEFAULT true,
    notes           TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_recipes_restaurant   ON recipes(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_recipes_item         ON recipes(item_id);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipe_id           UUID NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    ingredient_id       TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    quantity_required   NUMERIC(10,3) NOT NULL,
    unit                VARCHAR(50),
    waste_percent       NUMERIC(5,2) DEFAULT 0,  -- % lost during prep
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe     ON recipe_ingredients(recipe_id);
CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_ingredient ON recipe_ingredients(ingredient_id);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 4: INVENTORY LEDGER (Append-Only, Source of Truth)
-- Current stock = SUM(quantity_in) - SUM(quantity_out)
-- NEVER update stock directly; always append ledger entries.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS inventory_ledger (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES sub_branches(id),
    ingredient_id       TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    transaction_type    VARCHAR(30) NOT NULL CHECK (transaction_type IN (
                            'purchase', 'consumption', 'adjustment_in', 'adjustment_out',
                            'wastage', 'transfer_in', 'transfer_out', 'return', 'opening'
                        )),
    quantity_in          NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK (quantity_in >= 0),
    quantity_out         NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK (quantity_out >= 0),
    unit_cost            NUMERIC(10,2) DEFAULT 0,
    reference_type       VARCHAR(50),   -- order, purchase_order, grn, transfer, manual
    reference_id         TEXT,
    notes                TEXT,
    created_by           TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    CHECK (quantity_in > 0 OR quantity_out > 0)
);

CREATE INDEX IF NOT EXISTS idx_inv_ledger_restaurant     ON inventory_ledger(restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_ledger_branch         ON inventory_ledger(branch_id, created_at DESC) WHERE branch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inv_ledger_ingredient     ON inventory_ledger(ingredient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_ledger_ref            ON inventory_ledger(reference_type, reference_id) WHERE reference_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inv_ledger_type           ON inventory_ledger(transaction_type, created_at DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 5: VENDORS (Supplier Management)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS vendors (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    contact_person      VARCHAR(255),
    phone               VARCHAR(20),
    email               VARCHAR(255),
    address             TEXT,
    city                VARCHAR(100),
    state               VARCHAR(100),
    pincode             VARCHAR(10),
    gst_number          VARCHAR(20),
    pan_number          VARCHAR(20),
    bank_name           VARCHAR(255),
    bank_account_number VARCHAR(50),
    bank_ifsc           VARCHAR(20),
    payment_terms       INTEGER DEFAULT 30,      -- net days
    credit_limit        NUMERIC(14,2) DEFAULT 0,
    is_active           BOOLEAN DEFAULT true,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vendors_restaurant    ON vendors(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_vendors_gst           ON vendors(gst_number) WHERE gst_number IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 6: EXTEND PURCHASE ORDERS (link to vendors)
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE purchase_orders
    ADD COLUMN IF NOT EXISTS vendor_id       UUID,
    ADD COLUMN IF NOT EXISTS restaurant_id   UUID;

-- Cannot add FK with IF NOT EXISTS; use DO block
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_po_vendor'
    ) THEN
        ALTER TABLE purchase_orders
            ADD CONSTRAINT fk_po_vendor FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE SET NULL;
    END IF;
END $$;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 7: GOODS RECEIPT NOTES (GRN)
-- PO → GRN → Inventory Ledger → Journal Entry
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS goods_receipt_notes (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES sub_branches(id),
    purchase_order_id   INTEGER REFERENCES purchase_orders(id) ON DELETE SET NULL,
    vendor_id           UUID REFERENCES vendors(id) ON DELETE SET NULL,
    grn_number          VARCHAR(50) NOT NULL DEFAULT ('GRN-' || nextval('grn_number_seq')),
    received_date       DATE DEFAULT CURRENT_DATE,
    total_amount        NUMERIC(14,2) DEFAULT 0,
    status              VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft', 'verified', 'cancelled')),
    notes               TEXT,
    received_by         TEXT,
    verified_by         TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_grn_restaurant   ON goods_receipt_notes(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_grn_po           ON goods_receipt_notes(purchase_order_id) WHERE purchase_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_grn_vendor       ON goods_receipt_notes(vendor_id) WHERE vendor_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS grn_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grn_id              UUID NOT NULL REFERENCES goods_receipt_notes(id) ON DELETE CASCADE,
    ingredient_id       TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    ordered_quantity    NUMERIC(12,3) DEFAULT 0,
    received_quantity   NUMERIC(12,3) NOT NULL,
    rejected_quantity   NUMERIC(12,3) DEFAULT 0,
    unit                VARCHAR(50),
    unit_cost           NUMERIC(10,2) DEFAULT 0,
    line_total          NUMERIC(12,2) DEFAULT 0,
    batch_number        VARCHAR(100),
    expiry_date         DATE,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_grn_items_grn         ON grn_items(grn_id);
CREATE INDEX IF NOT EXISTS idx_grn_items_ingredient  ON grn_items(ingredient_id);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 8: VENDOR PAYMENTS
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS vendor_payments (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES sub_branches(id),
    vendor_id           UUID NOT NULL REFERENCES vendors(id) ON DELETE CASCADE,
    amount              NUMERIC(14,2) NOT NULL CHECK (amount > 0),
    payment_method      VARCHAR(30) NOT NULL CHECK (payment_method IN (
                            'cash', 'bank_transfer', 'cheque', 'upi', 'other'
                        )),
    payment_date        DATE DEFAULT CURRENT_DATE,
    reference_number    VARCHAR(100),       -- cheque/UTR/transaction ref
    purchase_order_id   INTEGER REFERENCES purchase_orders(id) ON DELETE SET NULL,
    grn_id              UUID REFERENCES goods_receipt_notes(id) ON DELETE SET NULL,
    notes               TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vendor_payments_vendor   ON vendor_payments(vendor_id);
CREATE INDEX IF NOT EXISTS idx_vendor_payments_date     ON vendor_payments(restaurant_id, payment_date DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 9: EXTEND ORDERS (COGS + order_type)
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS cost_of_goods_sold NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS order_type         VARCHAR(20) DEFAULT 'dine_in';

CREATE INDEX IF NOT EXISTS idx_orders_type ON orders(order_type) WHERE order_type IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 10: CASH DRAWERS & SHIFT MANAGEMENT
-- Detect cash mismatch, fraud, leakage.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS cash_drawers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
    name            VARCHAR(100) NOT NULL,
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cash_drawers_branch ON cash_drawers(branch_id);

CREATE TABLE IF NOT EXISTS shifts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
    drawer_id       UUID REFERENCES cash_drawers(id) ON DELETE SET NULL,
    user_id         TEXT NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    opening_cash    NUMERIC(12,2) NOT NULL DEFAULT 0,
    closing_cash    NUMERIC(12,2),           -- actual counted cash
    expected_cash   NUMERIC(12,2),           -- calculated from transactions
    cash_difference NUMERIC(12,2),           -- closing − expected (negative = shortage)
    status          VARCHAR(20) NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed', 'reconciled')),
    notes           TEXT,
    closed_by       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shifts_branch     ON shifts(branch_id, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_shifts_user       ON shifts(user_id, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_shifts_status     ON shifts(status) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS shift_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shift_id            UUID NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    transaction_type    VARCHAR(30) NOT NULL CHECK (transaction_type IN (
                            'sale', 'refund', 'expense', 'cash_in', 'cash_out', 'tip'
                        )),
    amount              NUMERIC(12,2) NOT NULL,
    payment_method      VARCHAR(30),
    reference_type      VARCHAR(50),
    reference_id        TEXT,
    description         TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shift_txn_shift ON shift_transactions(shift_id, created_at);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 11: INTER-BRANCH STOCK TRANSFERS
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS stock_transfers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    from_branch_id      UUID NOT NULL REFERENCES sub_branches(id),
    to_branch_id        UUID NOT NULL REFERENCES sub_branches(id),
    transfer_number     VARCHAR(50) NOT NULL DEFAULT ('TRF-' || nextval('transfer_number_seq')),
    status              VARCHAR(20) NOT NULL DEFAULT 'draft' CHECK (status IN (
                            'draft', 'approved', 'in_transit', 'received', 'cancelled'
                        )),
    requested_by        TEXT NOT NULL,
    approved_by         TEXT,
    received_by         TEXT,
    shipped_at          TIMESTAMPTZ,
    received_at         TIMESTAMPTZ,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    CHECK (from_branch_id != to_branch_id)
);

CREATE INDEX IF NOT EXISTS idx_stock_transfers_restaurant   ON stock_transfers(restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_status        ON stock_transfers(status);

CREATE TABLE IF NOT EXISTS stock_transfer_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transfer_id         UUID NOT NULL REFERENCES stock_transfers(id) ON DELETE CASCADE,
    ingredient_id       TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    quantity_sent       NUMERIC(12,3) NOT NULL CHECK (quantity_sent > 0),
    quantity_received   NUMERIC(12,3),
    unit                VARCHAR(50),
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_transfer_items_transfer ON stock_transfer_items(transfer_id);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 12: GST TAX RATES (India-Compliant)
-- Supports CGST+SGST (intra-state) and IGST (inter-state)
-- Rates: 0%, 5%, 12%, 18%, 28%
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tax_rates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name                VARCHAR(100) NOT NULL,        -- e.g. "GST 5%"
    hsn_code            VARCHAR(20),
    rate_percentage     NUMERIC(5,2) NOT NULL,         -- total rate (e.g. 5.00)
    cgst_percentage     NUMERIC(5,2) NOT NULL DEFAULT 0,
    sgst_percentage     NUMERIC(5,2) NOT NULL DEFAULT 0,
    igst_percentage     NUMERIC(5,2) NOT NULL DEFAULT 0,
    is_inclusive        BOOLEAN DEFAULT false,          -- true = price includes tax
    applicable_on       VARCHAR(30) DEFAULT 'all' CHECK (applicable_on IN (
                            'food', 'beverage', 'service', 'combo', 'alcohol', 'all'
                        )),
    is_exempt           BOOLEAN DEFAULT false,          -- e.g. alcohol (state tax, no GST)
    is_composition      BOOLEAN DEFAULT false,          -- composition scheme flag
    is_active           BOOLEAN DEFAULT true,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_rates_restaurant ON tax_rates(restaurant_id);

-- ── Item → Tax mapping ──
CREATE TABLE IF NOT EXISTS item_tax_mapping (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id         INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
    tax_rate_id     UUID NOT NULL REFERENCES tax_rates(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(item_id, tax_rate_id)
);

CREATE INDEX IF NOT EXISTS idx_item_tax_item     ON item_tax_mapping(item_id);
CREATE INDEX IF NOT EXISTS idx_item_tax_rate     ON item_tax_mapping(tax_rate_id);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 13: ORDER TAX DETAILS (snapshot at order time)
-- Precomputed; DO NOT recalculate after order placement.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS order_tax_details (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id            UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    tax_rate_id         UUID REFERENCES tax_rates(id),
    tax_name            VARCHAR(100),
    rate_percentage     NUMERIC(5,2) NOT NULL DEFAULT 0,
    taxable_amount      NUMERIC(12,2) NOT NULL DEFAULT 0,
    cgst_amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
    sgst_amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
    igst_amount         NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_tax           NUMERIC(12,2) NOT NULL DEFAULT 0,
    is_inclusive        BOOLEAN DEFAULT false,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_order_tax_order ON order_tax_details(order_id);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 14: EXTEND INVOICES WITH GST FIELDS
-- Backward compatible: all new columns are nullable/defaulted.
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS restaurant_id      UUID,
    ADD COLUMN IF NOT EXISTS branch_id          UUID,
    ADD COLUMN IF NOT EXISTS gstin              VARCHAR(20),
    ADD COLUMN IF NOT EXISTS customer_gstin     VARCHAR(20),
    ADD COLUMN IF NOT EXISTS place_of_supply    VARCHAR(50),
    ADD COLUMN IF NOT EXISTS invoice_type       VARCHAR(10) DEFAULT 'B2C',
    ADD COLUMN IF NOT EXISTS taxable_amount     NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cgst_amount        NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sgst_amount        NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS igst_amount        NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS discount_amount    NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS round_off          NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS is_cancelled       BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS cancelled_at       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ DEFAULT NOW();

-- GST-compliant invoice line items
CREATE TABLE IF NOT EXISTS gst_invoice_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id          INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    item_id             INTEGER REFERENCES items("Item_ID") ON DELETE SET NULL,
    item_name           VARCHAR(255) NOT NULL,
    hsn_code            VARCHAR(20),
    quantity            INTEGER NOT NULL DEFAULT 1,
    unit_price          NUMERIC(10,2) NOT NULL,
    discount            NUMERIC(10,2) DEFAULT 0,
    taxable_value       NUMERIC(12,2) NOT NULL,
    cgst_rate           NUMERIC(5,2) DEFAULT 0,
    cgst_amount         NUMERIC(12,2) DEFAULT 0,
    sgst_rate           NUMERIC(5,2) DEFAULT 0,
    sgst_amount         NUMERIC(12,2) DEFAULT 0,
    igst_rate           NUMERIC(5,2) DEFAULT 0,
    igst_amount         NUMERIC(12,2) DEFAULT 0,
    total_amount        NUMERIC(12,2) NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gst_invoice_items_invoice ON gst_invoice_items(invoice_id);

-- Invoice number index for sequential validation
CREATE INDEX IF NOT EXISTS idx_invoices_number_user ON invoices(user_id, invoice_number);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 15: GST REPORTS (Filing-Ready Aggregation)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS gst_reports (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    report_type         VARCHAR(20) NOT NULL CHECK (report_type IN ('GSTR1', 'GSTR3B', 'tax_liability')),
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    total_sales         NUMERIC(14,2) DEFAULT 0,
    total_taxable       NUMERIC(14,2) DEFAULT 0,
    cgst_total          NUMERIC(14,2) DEFAULT 0,
    sgst_total          NUMERIC(14,2) DEFAULT 0,
    igst_total          NUMERIC(14,2) DEFAULT 0,
    total_tax           NUMERIC(14,2) DEFAULT 0,
    b2b_count           INTEGER DEFAULT 0,
    b2c_count           INTEGER DEFAULT 0,
    report_data         JSONB,          -- detailed breakdown for filing
    status              VARCHAR(20) DEFAULT 'draft' CHECK (status IN ('draft', 'generated', 'filed')),
    generated_at        TIMESTAMPTZ,
    filed_at            TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, report_type, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_gst_reports_restaurant  ON gst_reports(restaurant_id, period_start DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 16: ANALYTICS TABLES (Profitability + P&L)
-- Populated async by analytics aggregation jobs.
-- ════════════════════════════════════════════════════════════════════════════

-- Per-item profitability for a period
CREATE TABLE IF NOT EXISTS item_profitability (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
    item_id             INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    quantity_sold       INTEGER DEFAULT 0,
    total_revenue       NUMERIC(14,2) DEFAULT 0,
    total_cogs          NUMERIC(14,2) DEFAULT 0,
    gross_profit        NUMERIC(14,2) DEFAULT 0,
    margin_percent      NUMERIC(5,2) DEFAULT 0,
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, branch_id, item_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_item_profit_branch  ON item_profitability(branch_id, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_item_profit_item    ON item_profitability(item_id);

-- Daily profit & loss per branch
CREATE TABLE IF NOT EXISTS daily_pnl (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
    pnl_date            DATE NOT NULL,
    total_revenue       NUMERIC(14,2) DEFAULT 0,
    total_cogs          NUMERIC(14,2) DEFAULT 0,
    gross_profit        NUMERIC(14,2) DEFAULT 0,
    operating_expenses  NUMERIC(14,2) DEFAULT 0,
    net_profit          NUMERIC(14,2) DEFAULT 0,
    tax_collected       NUMERIC(14,2) DEFAULT 0,
    total_orders        INTEGER DEFAULT 0,
    avg_order_value     NUMERIC(12,2) DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, branch_id, pnl_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_pnl_branch_date ON daily_pnl(branch_id, pnl_date DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 17: USEFUL VIEWS (Read-Only, No Write Overhead)
-- ════════════════════════════════════════════════════════════════════════════

-- Current ingredient stock derived from ledger (per branch)
CREATE OR REPLACE VIEW v_ingredient_stock_ledger AS
SELECT
    il.restaurant_id,
    il.branch_id,
    il.ingredient_id,
    i.name AS ingredient_name,
    i.unit,
    COALESCE(SUM(il.quantity_in), 0) - COALESCE(SUM(il.quantity_out), 0) AS current_stock,
    CASE WHEN SUM(il.quantity_in) > 0
         THEN SUM(il.quantity_in * il.unit_cost) / SUM(il.quantity_in)
         ELSE 0
    END AS weighted_avg_cost,
    MAX(il.created_at) AS last_movement_at
FROM inventory_ledger il
JOIN ingredients i ON i.id = il.ingredient_id
GROUP BY il.restaurant_id, il.branch_id, il.ingredient_id, i.name, i.unit;

-- Account balances from journal entries
CREATE OR REPLACE VIEW v_account_balances AS
SELECT
    coa.restaurant_id,
    coa.id          AS account_id,
    coa.account_code,
    coa.name,
    coa.account_type,
    COALESCE(SUM(jl.debit), 0)  AS total_debit,
    COALESCE(SUM(jl.credit), 0) AS total_credit,
    CASE
        WHEN coa.account_type IN ('asset', 'expense')
            THEN COALESCE(SUM(jl.debit), 0) - COALESCE(SUM(jl.credit), 0)
        ELSE COALESCE(SUM(jl.credit), 0) - COALESCE(SUM(jl.debit), 0)
    END AS balance
FROM chart_of_accounts coa
LEFT JOIN journal_lines jl ON jl.account_id = coa.id
LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id AND je.is_reversed = false
WHERE coa.is_active = true
GROUP BY coa.restaurant_id, coa.id, coa.account_code, coa.name, coa.account_type;

-- Vendor outstanding balances
CREATE OR REPLACE VIEW v_vendor_balances AS
SELECT
    v.restaurant_id,
    v.id            AS vendor_id,
    v.name,
    v.gst_number,
    COALESCE(grn_totals.total_purchased, 0) AS total_purchased,
    COALESCE(pay_totals.total_paid, 0)      AS total_paid,
    COALESCE(grn_totals.total_purchased, 0) - COALESCE(pay_totals.total_paid, 0) AS balance_due
FROM vendors v
LEFT JOIN LATERAL (
    SELECT SUM(g.total_amount) AS total_purchased
    FROM goods_receipt_notes g
    WHERE g.vendor_id = v.id AND g.status = 'verified'
) grn_totals ON true
LEFT JOIN LATERAL (
    SELECT SUM(vp.amount) AS total_paid
    FROM vendor_payments vp
    WHERE vp.vendor_id = v.id
) pay_totals ON true;

-- Trial balance (all accounts with balances)
CREATE OR REPLACE VIEW v_trial_balance AS
SELECT
    restaurant_id,
    account_code,
    name,
    account_type,
    CASE WHEN balance >= 0 AND account_type IN ('asset', 'expense') THEN balance ELSE 0 END AS debit_balance,
    CASE WHEN balance >= 0 AND account_type IN ('liability', 'equity', 'revenue') THEN balance ELSE 0 END AS credit_balance
FROM v_account_balances
WHERE balance != 0
ORDER BY account_code;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 18: SEED FUNCTION — Default Chart of Accounts per Restaurant
-- Call fn_seed_chart_of_accounts(restaurant_id) when onboarding.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_seed_chart_of_accounts(p_restaurant_id UUID)
RETURNS void AS $$
DECLARE
    v_asset_id      UUID;
    v_liability_id  UUID;
    v_equity_id     UUID;
    v_revenue_id    UUID;
    v_expense_id    UUID;
BEGIN
    -- Skip if already seeded
    IF EXISTS (SELECT 1 FROM chart_of_accounts WHERE restaurant_id = p_restaurant_id LIMIT 1) THEN
        RETURN;
    END IF;

    -- ── ASSET accounts ──
    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '1000', 'Assets', 'asset', true)
    RETURNING id INTO v_asset_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system) VALUES
        (p_restaurant_id, '1001', 'Cash',                   'asset', v_asset_id, true),
        (p_restaurant_id, '1002', 'Bank Account',           'asset', v_asset_id, true),
        (p_restaurant_id, '1003', 'Accounts Receivable',    'asset', v_asset_id, true),
        (p_restaurant_id, '1004', 'Inventory - Food',       'asset', v_asset_id, true),
        (p_restaurant_id, '1005', 'Inventory - Beverages',  'asset', v_asset_id, true),
        (p_restaurant_id, '1006', 'Prepaid Expenses',       'asset', v_asset_id, false);

    -- ── LIABILITY accounts ──
    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '2000', 'Liabilities', 'liability', true)
    RETURNING id INTO v_liability_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system) VALUES
        (p_restaurant_id, '2001', 'Accounts Payable',   'liability', v_liability_id, true),
        (p_restaurant_id, '2002', 'CGST Payable',       'liability', v_liability_id, true),
        (p_restaurant_id, '2003', 'SGST Payable',       'liability', v_liability_id, true),
        (p_restaurant_id, '2004', 'IGST Payable',       'liability', v_liability_id, true),
        (p_restaurant_id, '2005', 'Salary Payable',     'liability', v_liability_id, false),
        (p_restaurant_id, '2006', 'Other Payables',     'liability', v_liability_id, false);

    -- ── EQUITY accounts ──
    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '3000', 'Equity', 'equity', true)
    RETURNING id INTO v_equity_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system) VALUES
        (p_restaurant_id, '3001', 'Owner Capital',      'equity', v_equity_id, true),
        (p_restaurant_id, '3002', 'Retained Earnings',  'equity', v_equity_id, true);

    -- ── REVENUE accounts ──
    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '4000', 'Revenue', 'revenue', true)
    RETURNING id INTO v_revenue_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system) VALUES
        (p_restaurant_id, '4001', 'Food Sales',        'revenue', v_revenue_id, true),
        (p_restaurant_id, '4002', 'Beverage Sales',    'revenue', v_revenue_id, true),
        (p_restaurant_id, '4003', 'Delivery Income',   'revenue', v_revenue_id, false),
        (p_restaurant_id, '4004', 'Service Charges',   'revenue', v_revenue_id, false),
        (p_restaurant_id, '4005', 'Other Income',      'revenue', v_revenue_id, false);

    -- ── EXPENSE accounts ──
    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, is_system)
    VALUES (p_restaurant_id, '5000', 'Expenses', 'expense', true)
    RETURNING id INTO v_expense_id;

    INSERT INTO chart_of_accounts (restaurant_id, account_code, name, account_type, parent_id, is_system) VALUES
        (p_restaurant_id, '5001', 'COGS - Food',           'expense', v_expense_id, true),
        (p_restaurant_id, '5002', 'COGS - Beverages',      'expense', v_expense_id, true),
        (p_restaurant_id, '5003', 'Staff Salaries',        'expense', v_expense_id, false),
        (p_restaurant_id, '5004', 'Rent',                  'expense', v_expense_id, false),
        (p_restaurant_id, '5005', 'Utilities',             'expense', v_expense_id, false),
        (p_restaurant_id, '5006', 'Marketing',             'expense', v_expense_id, false),
        (p_restaurant_id, '5007', 'Packaging',             'expense', v_expense_id, false),
        (p_restaurant_id, '5008', 'Delivery Charges',      'expense', v_expense_id, false),
        (p_restaurant_id, '5009', 'Platform Commissions',  'expense', v_expense_id, false),
        (p_restaurant_id, '5010', 'Miscellaneous',         'expense', v_expense_id, false);

END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 19: SEED FUNCTION — Default GST Tax Rates per Restaurant
-- Call fn_seed_default_tax_rates(restaurant_id) when onboarding.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_seed_default_tax_rates(p_restaurant_id UUID)
RETURNS void AS $$
BEGIN
    -- Skip if already seeded
    IF EXISTS (SELECT 1 FROM tax_rates WHERE restaurant_id = p_restaurant_id LIMIT 1) THEN
        RETURN;
    END IF;

    INSERT INTO tax_rates
        (restaurant_id, name, rate_percentage, cgst_percentage, sgst_percentage, igst_percentage, applicable_on, is_inclusive)
    VALUES
        -- Standard restaurant rates (intra-state split)
        (p_restaurant_id, 'GST 0% (Exempt)',           0,     0,    0,    0,     'food',     false),
        (p_restaurant_id, 'GST 5% (Restaurant)',        5,     2.5,  2.5,  0,     'food',     false),
        (p_restaurant_id, 'GST 5% (Restaurant) Incl',   5,     2.5,  2.5,  0,     'food',     true),
        (p_restaurant_id, 'GST 12%',                    12,    6,    6,    0,     'food',     false),
        (p_restaurant_id, 'GST 18%',                    18,    9,    9,    0,     'service',  false),
        (p_restaurant_id, 'GST 28% (Luxury)',           28,    14,   14,   0,     'food',     false),
        -- Inter-state rates (IGST only)
        (p_restaurant_id, 'IGST 5%',                    5,     0,    0,    5,     'food',     false),
        (p_restaurant_id, 'IGST 12%',                   12,    0,    0,    12,    'food',     false),
        (p_restaurant_id, 'IGST 18%',                   18,    0,    0,    18,    'service',  false),
        -- Alcohol (exempt from GST, state-taxed separately)
        (p_restaurant_id, 'No GST (Alcohol)',           0,     0,    0,    0,     'alcohol',  false),
        -- Composition scheme
        (p_restaurant_id, 'Composition 5%',             5,     2.5,  2.5,  0,     'all',      true);

    -- Mark alcohol as exempt
    UPDATE tax_rates SET is_exempt = true
    WHERE restaurant_id = p_restaurant_id AND applicable_on = 'alcohol';

    -- Mark composition scheme
    UPDATE tax_rates SET is_composition = true
    WHERE restaurant_id = p_restaurant_id AND name LIKE 'Composition%';
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 20: TAX CALCULATION HELPER FUNCTIONS
-- ════════════════════════════════════════════════════════════════════════════

-- Calculate tax for a given price and tax_rate_id
-- Returns: taxable_amount, cgst, sgst, igst, total_tax, grand_total
CREATE OR REPLACE FUNCTION fn_calculate_tax(
    p_price         NUMERIC,
    p_tax_rate_id   UUID,
    p_is_interstate BOOLEAN DEFAULT false
) RETURNS TABLE (
    taxable_amount  NUMERIC(12,2),
    cgst_amount     NUMERIC(12,2),
    sgst_amount     NUMERIC(12,2),
    igst_amount     NUMERIC(12,2),
    total_tax       NUMERIC(12,2),
    grand_total     NUMERIC(12,2)
) AS $$
DECLARE
    v_rate          RECORD;
    v_taxable       NUMERIC(12,2);
    v_cgst          NUMERIC(12,2) := 0;
    v_sgst          NUMERIC(12,2) := 0;
    v_igst          NUMERIC(12,2) := 0;
    v_total_tax     NUMERIC(12,2) := 0;
BEGIN
    SELECT * INTO v_rate FROM tax_rates WHERE id = p_tax_rate_id;

    IF NOT FOUND OR v_rate.is_exempt THEN
        RETURN QUERY SELECT p_price, 0::NUMERIC(12,2), 0::NUMERIC(12,2),
                            0::NUMERIC(12,2), 0::NUMERIC(12,2), p_price;
        RETURN;
    END IF;

    IF v_rate.is_inclusive THEN
        -- Extract tax from inclusive price: taxable = price / (1 + rate/100)
        v_taxable := ROUND(p_price / (1 + v_rate.rate_percentage / 100), 2);
    ELSE
        v_taxable := p_price;
    END IF;

    IF p_is_interstate THEN
        -- IGST for inter-state
        v_igst := ROUND(v_taxable * v_rate.igst_percentage / 100, 2);
    ELSE
        -- CGST + SGST for intra-state
        v_cgst := ROUND(v_taxable * v_rate.cgst_percentage / 100, 2);
        v_sgst := ROUND(v_taxable * v_rate.sgst_percentage / 100, 2);
    END IF;

    v_total_tax := v_cgst + v_sgst + v_igst;

    RETURN QUERY SELECT v_taxable, v_cgst, v_sgst, v_igst, v_total_tax,
                        CASE WHEN v_rate.is_inclusive THEN p_price ELSE v_taxable + v_total_tax END;
END;
$$ LANGUAGE plpgsql STABLE;


-- Calculate COGS for an item using recipes, falling back to item_ingredients
CREATE OR REPLACE FUNCTION fn_calculate_item_cogs(
    p_item_id   INTEGER,
    p_quantity  INTEGER DEFAULT 1
) RETURNS NUMERIC(12,2) AS $$
DECLARE
    v_cogs NUMERIC(12,2) := 0;
BEGIN
    -- Try recipes first (new ERP system)
    SELECT COALESCE(SUM(
        ri.quantity_required * (1 + ri.waste_percent / 100) * COALESCE(i.cost_per_unit, 0)
    ), 0) INTO v_cogs
    FROM recipe_ingredients ri
    JOIN recipes r ON r.id = ri.recipe_id AND r.is_active = true
    JOIN ingredients i ON i.id = ri.ingredient_id
    WHERE r.item_id = p_item_id;

    -- Fallback to item_ingredients (legacy system)
    IF v_cogs = 0 THEN
        SELECT COALESCE(SUM(
            ii.quantity_used * COALESCE(i.cost_per_unit, 0)
        ), 0) INTO v_cogs
        FROM item_ingredients ii
        JOIN ingredients i ON i.id = ii.ingredient_id
        WHERE ii.item_id = p_item_id;
    END IF;

    RETURN v_cogs * p_quantity;
END;
$$ LANGUAGE plpgsql STABLE;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 21: GSTIN VALIDATION FUNCTION
-- Format: 2-digit state code + 10-char PAN + 1 digit + Z + 1 check
-- Example: 27AABCU9603R1ZM
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_validate_gstin(p_gstin TEXT)
RETURNS BOOLEAN AS $$
BEGIN
    IF p_gstin IS NULL OR LENGTH(p_gstin) = 0 THEN
        RETURN true;  -- NULL/empty is OK (B2C)
    END IF;
    -- Basic format: 15 chars, starts with 2-digit state code (01-37)
    RETURN p_gstin ~ '^[0-3][0-9][A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$';
END;
$$ LANGUAGE plpgsql IMMUTABLE;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 22: ADDITIONAL PERFORMANCE INDEXES
-- ════════════════════════════════════════════════════════════════════════════

-- Composite indexes for common ERP queries
CREATE INDEX IF NOT EXISTS idx_je_restaurant_ref_type
    ON journal_entries(restaurant_id, reference_type, entry_date DESC);

CREATE INDEX IF NOT EXISTS idx_jl_account_entry
    ON journal_lines(account_id, journal_entry_id);

CREATE INDEX IF NOT EXISTS idx_inv_ledger_stock_calc
    ON inventory_ledger(ingredient_id, branch_id)
    INCLUDE (quantity_in, quantity_out);

CREATE INDEX IF NOT EXISTS idx_order_tax_details_order
    ON order_tax_details(order_id);

CREATE INDEX IF NOT EXISTS idx_orders_cogs
    ON orders(restaurant_id, created_at DESC)
    WHERE cost_of_goods_sold > 0;

CREATE INDEX IF NOT EXISTS idx_recipes_active
    ON recipes(item_id)
    WHERE is_active = true;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 23: GST REPORT AGGREGATION FUNCTION
-- Generates GSTR-1/3B summary for a period.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_generate_gst_report(
    p_restaurant_id UUID,
    p_report_type   VARCHAR,
    p_period_start  DATE,
    p_period_end    DATE
) RETURNS UUID AS $$
DECLARE
    v_report_id UUID;
    v_data      RECORD;
BEGIN
    SELECT
        COALESCE(SUM(i.amount), 0)          AS total_sales,
        COALESCE(SUM(i.taxable_amount), 0)  AS total_taxable,
        COALESCE(SUM(i.cgst_amount), 0)     AS cgst_total,
        COALESCE(SUM(i.sgst_amount), 0)     AS sgst_total,
        COALESCE(SUM(i.igst_amount), 0)     AS igst_total,
        COALESCE(SUM(i.cgst_amount + i.sgst_amount + i.igst_amount), 0) AS total_tax,
        COUNT(*) FILTER (WHERE i.invoice_type = 'B2B') AS b2b_count,
        COUNT(*) FILTER (WHERE i.invoice_type = 'B2C') AS b2c_count
    INTO v_data
    FROM invoices i
    WHERE i.restaurant_id = p_restaurant_id
      AND i.is_cancelled = false
      AND i.created_at::DATE BETWEEN p_period_start AND p_period_end;

    INSERT INTO gst_reports
        (restaurant_id, report_type, period_start, period_end,
         total_sales, total_taxable, cgst_total, sgst_total, igst_total, total_tax,
         b2b_count, b2c_count, status, generated_at)
    VALUES
        (p_restaurant_id, p_report_type, p_period_start, p_period_end,
         v_data.total_sales, v_data.total_taxable, v_data.cgst_total, v_data.sgst_total,
         v_data.igst_total, v_data.total_tax,
         v_data.b2b_count, v_data.b2c_count, 'generated', NOW())
    ON CONFLICT (restaurant_id, report_type, period_start, period_end)
    DO UPDATE SET
        total_sales    = EXCLUDED.total_sales,
        total_taxable  = EXCLUDED.total_taxable,
        cgst_total     = EXCLUDED.cgst_total,
        sgst_total     = EXCLUDED.sgst_total,
        igst_total     = EXCLUDED.igst_total,
        total_tax      = EXCLUDED.total_tax,
        b2b_count      = EXCLUDED.b2b_count,
        b2c_count      = EXCLUDED.b2c_count,
        status         = 'generated',
        generated_at   = NOW(),
        updated_at     = NOW()
    RETURNING id INTO v_report_id;

    RETURN v_report_id;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 24: DAILY P&L AGGREGATION FUNCTION
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_aggregate_daily_pnl(
    p_restaurant_id UUID,
    p_branch_id     UUID,
    p_date          DATE
) RETURNS void AS $$
DECLARE
    v_revenue       NUMERIC(14,2) := 0;
    v_cogs          NUMERIC(14,2) := 0;
    v_expenses      NUMERIC(14,2) := 0;
    v_tax           NUMERIC(14,2) := 0;
    v_orders        INTEGER := 0;
    v_avg_order     NUMERIC(12,2) := 0;
BEGIN
    -- Revenue from completed orders
    SELECT
        COALESCE(SUM(total_amount), 0),
        COUNT(*),
        COALESCE(AVG(total_amount), 0),
        COALESCE(SUM(cost_of_goods_sold), 0),
        COALESCE(SUM(tax_amount), 0)
    INTO v_revenue, v_orders, v_avg_order, v_cogs, v_tax
    FROM orders
    WHERE restaurant_id = p_restaurant_id::TEXT
      AND branch_id = p_branch_id
      AND DATE(created_at) = p_date
      AND status IN ('Served', 'Delivered', 'completed');

    -- Operating expenses from accounting_entries
    SELECT COALESCE(SUM(ABS(amount)), 0) INTO v_expenses
    FROM accounting_entries
    WHERE restaurant_id = p_restaurant_id::TEXT
      AND branch_id = p_branch_id
      AND entry_type = 'expense'
      AND DATE(created_at) = p_date;

    INSERT INTO daily_pnl
        (restaurant_id, branch_id, pnl_date, total_revenue, total_cogs,
         gross_profit, operating_expenses, net_profit, tax_collected,
         total_orders, avg_order_value)
    VALUES
        (p_restaurant_id, p_branch_id, p_date, v_revenue, v_cogs,
         v_revenue - v_cogs, v_expenses, v_revenue - v_cogs - v_expenses, v_tax,
         v_orders, v_avg_order)
    ON CONFLICT (restaurant_id, branch_id, pnl_date)
    DO UPDATE SET
        total_revenue      = EXCLUDED.total_revenue,
        total_cogs         = EXCLUDED.total_cogs,
        gross_profit       = EXCLUDED.gross_profit,
        operating_expenses = EXCLUDED.operating_expenses,
        net_profit         = EXCLUDED.net_profit,
        tax_collected      = EXCLUDED.tax_collected,
        total_orders       = EXCLUDED.total_orders,
        avg_order_value    = EXCLUDED.avg_order_value,
        updated_at         = NOW();
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- END OF MIGRATION 006
-- ════════════════════════════════════════════════════════════════════════════

-- Post-migration steps (run manually per restaurant):
--   SELECT fn_seed_chart_of_accounts('your-restaurant-uuid');
--   SELECT fn_seed_default_tax_rates('your-restaurant-uuid');
