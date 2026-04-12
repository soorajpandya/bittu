-- ════════════════════════════════════════════════════════════════════════════
-- MIGRATION 007: Connect, Harden & Upgrade ERP
-- Non-breaking additions only. All columns nullable/defaulted.
-- Run in Supabase SQL Editor.
-- ════════════════════════════════════════════════════════════════════════════

-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 1: INGREDIENTS — Add reorder_level (missing, caused runtime bugs)
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE ingredients
    ADD COLUMN IF NOT EXISTS reorder_level   NUMERIC(12,3) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS category        VARCHAR(100),
    ADD COLUMN IF NOT EXISTS storage_type    VARCHAR(30) DEFAULT 'dry'
        CHECK (storage_type IN ('dry','cold','frozen','ambient'));


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 2: TAX RULE ENGINE
-- Dynamic rule matching: priority → order_type → platform → is_interstate
-- Fallback: item_tax_mapping → restaurant default
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS tax_rules (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name                VARCHAR(255) NOT NULL,
    priority            INTEGER NOT NULL DEFAULT 100,   -- lower = higher priority
    tax_rate_id         UUID NOT NULL REFERENCES tax_rates(id) ON DELETE CASCADE,

    -- Match conditions (NULL = match any)
    order_type          VARCHAR(20),                    -- dine_in, takeaway, delivery, NULL=all
    platform            VARCHAR(50),                    -- swiggy, zomato, direct, NULL=all
    is_interstate       BOOLEAN,                        -- true=IGST, false=CGST+SGST, NULL=auto
    applicable_on       VARCHAR(30),                    -- food, beverage, alcohol, service, combo, NULL=all
    min_order_value     NUMERIC(12,2),                  -- apply only above this amount
    max_order_value     NUMERIC(12,2),                  -- apply only below this amount
    time_from           TIME,                           -- happy hour start
    time_to             TIME,                           -- happy hour end

    is_active           BOOLEAN DEFAULT true,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tax_rules_restaurant
    ON tax_rules(restaurant_id, priority);
CREATE INDEX IF NOT EXISTS idx_tax_rules_active
    ON tax_rules(restaurant_id) WHERE is_active = true;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 3: PLATFORM TAX CONFIG
-- Aggregator platforms (Swiggy, Zomato) handle GST collection.
-- When gst_handled_by_platform = true, skip GST calc, mark as external.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS platform_tax_config (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id           UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    platform                VARCHAR(50) NOT NULL,       -- swiggy, zomato, magicpin, etc.
    gst_handled_by_platform BOOLEAN NOT NULL DEFAULT false,
    commission_rate         NUMERIC(5,2) DEFAULT 0,     -- platform commission %
    tcs_rate                NUMERIC(5,2) DEFAULT 0,     -- TCS (Tax Collected at Source) %
    notes                   TEXT,
    is_active               BOOLEAN DEFAULT true,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_platform_tax_restaurant
    ON platform_tax_config(restaurant_id);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 4: LINK accounting_entries → journal_entries (backward compat)
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE accounting_entries
    ADD COLUMN IF NOT EXISTS journal_entry_id UUID REFERENCES journal_entries(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS restaurant_id_uuid UUID REFERENCES restaurants(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_acct_entries_journal
    ON accounting_entries(journal_entry_id) WHERE journal_entry_id IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 5: ORDER ITEMS — per-item tax tracking
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS tax_rate_id    UUID REFERENCES tax_rates(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS taxable_amount NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cgst_amount    NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sgst_amount    NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS igst_amount    NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS tax_total      NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS hsn_code       VARCHAR(20),
    ADD COLUMN IF NOT EXISTS discount_amount NUMERIC(12,2) DEFAULT 0;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 6: ORDERS — platform tracking + GST flags
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS platform           VARCHAR(50) DEFAULT 'direct',
    ADD COLUMN IF NOT EXISTS is_interstate      BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS gst_handled_externally BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS invoice_id         INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS shift_id           UUID REFERENCES shifts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_orders_platform
    ON orders(restaurant_id, platform) WHERE platform IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_orders_shift
    ON orders(shift_id) WHERE shift_id IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 7: ERP EVENT LOG — audit trail for event processing
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS erp_event_log (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID REFERENCES restaurants(id) ON DELETE CASCADE,
    event_type          VARCHAR(100) NOT NULL,
    reference_type      VARCHAR(50),
    reference_id        TEXT,
    status              VARCHAR(20) NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('completed','failed','skipped')),
    payload             JSONB,
    error_message       TEXT,
    processing_time_ms  INTEGER,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_erp_event_log_type
    ON erp_event_log(event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_erp_event_log_ref
    ON erp_event_log(reference_type, reference_id) WHERE reference_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_erp_event_log_failed
    ON erp_event_log(status) WHERE status = 'failed';


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 8: FEATURE FLAGS — safe rollout
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS feature_flags (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID REFERENCES restaurants(id) ON DELETE CASCADE,
    flag_name           VARCHAR(100) NOT NULL,
    is_enabled          BOOLEAN NOT NULL DEFAULT false,
    metadata            JSONB,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(restaurant_id, flag_name)
);

CREATE INDEX IF NOT EXISTS idx_feature_flags_lookup
    ON feature_flags(restaurant_id, flag_name);


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 9: INVOICES — link to order_items for GST line-level reconciliation
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE invoices
    ADD COLUMN IF NOT EXISTS total_amount   NUMERIC(12,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS irn            VARCHAR(100),    -- e-Invoice IRN
    ADD COLUMN IF NOT EXISTS ack_number     VARCHAR(100),    -- e-Invoice acknowledgment
    ADD COLUMN IF NOT EXISTS ack_date       TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS qr_code        TEXT;            -- e-Invoice QR


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 10: VENDOR PURCHASE TRACKING — link vendor_payments to accounting
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE vendor_payments
    ADD COLUMN IF NOT EXISTS journal_entry_id UUID REFERENCES journal_entries(id) ON DELETE SET NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 11: FUNCTION — Tax Rule Resolution
-- Resolves the correct tax_rate for a given context.
-- Priority: tax_rules (by priority) → item_tax_mapping → restaurant default
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_resolve_tax_rate(
    p_restaurant_id UUID,
    p_item_id       INTEGER,
    p_order_type    VARCHAR DEFAULT NULL,
    p_platform      VARCHAR DEFAULT NULL,
    p_is_interstate BOOLEAN DEFAULT false,
    p_category      VARCHAR DEFAULT NULL,
    p_order_value   NUMERIC DEFAULT 0,
    p_current_time  TIME DEFAULT CURRENT_TIME
)
RETURNS UUID AS $$
DECLARE
    v_tax_rate_id UUID;
BEGIN
    -- 1. Try tax_rules (priority-ordered, most specific wins)
    SELECT tr.tax_rate_id INTO v_tax_rate_id
    FROM tax_rules tr
    WHERE tr.restaurant_id = p_restaurant_id
      AND tr.is_active = true
      AND (tr.order_type IS NULL OR tr.order_type = p_order_type)
      AND (tr.platform IS NULL OR tr.platform = p_platform)
      AND (tr.is_interstate IS NULL OR tr.is_interstate = p_is_interstate)
      AND (tr.applicable_on IS NULL OR tr.applicable_on = p_category OR tr.applicable_on = 'all')
      AND (tr.min_order_value IS NULL OR p_order_value >= tr.min_order_value)
      AND (tr.max_order_value IS NULL OR p_order_value <= tr.max_order_value)
      AND (tr.time_from IS NULL OR p_current_time >= tr.time_from)
      AND (tr.time_to IS NULL OR p_current_time <= tr.time_to)
    ORDER BY tr.priority ASC
    LIMIT 1;

    IF v_tax_rate_id IS NOT NULL THEN
        RETURN v_tax_rate_id;
    END IF;

    -- 2. Fallback: item_tax_mapping
    SELECT itm.tax_rate_id INTO v_tax_rate_id
    FROM item_tax_mapping itm
    JOIN tax_rates t ON t.id = itm.tax_rate_id AND t.is_active = true
    WHERE itm.item_id = p_item_id
    LIMIT 1;

    IF v_tax_rate_id IS NOT NULL THEN
        RETURN v_tax_rate_id;
    END IF;

    -- 3. Fallback: restaurant default (GST 5% for restaurants)
    SELECT id INTO v_tax_rate_id
    FROM tax_rates
    WHERE restaurant_id = p_restaurant_id
      AND is_active = true
      AND rate_percentage = 5
      AND is_composition = false
      AND igst_percentage = 0
    LIMIT 1;

    RETURN v_tax_rate_id;
END;
$$ LANGUAGE plpgsql STABLE;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 12: FUNCTION — Create GST Invoice from Order
-- Atomically creates invoice + gst_invoice_items from order + order_items.
-- Called once at order creation; result is IMMUTABLE.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_create_gst_invoice(
    p_order_id          UUID,
    p_restaurant_id     UUID,
    p_branch_id         UUID DEFAULT NULL,
    p_gstin             VARCHAR DEFAULT NULL,
    p_customer_gstin    VARCHAR DEFAULT NULL,
    p_place_of_supply   VARCHAR DEFAULT NULL,
    p_is_interstate     BOOLEAN DEFAULT false
)
RETURNS INTEGER AS $$
DECLARE
    v_invoice_id    INTEGER;
    v_invoice_num   VARCHAR;
    v_order         RECORD;
    v_item          RECORD;
    v_tax_rate      RECORD;
    v_taxable       NUMERIC(12,2);
    v_cgst          NUMERIC(12,2);
    v_sgst          NUMERIC(12,2);
    v_igst          NUMERIC(12,2);
    v_total_taxable NUMERIC(12,2) := 0;
    v_total_cgst    NUMERIC(12,2) := 0;
    v_total_sgst    NUMERIC(12,2) := 0;
    v_total_igst    NUMERIC(12,2) := 0;
    v_user_id       TEXT;
BEGIN
    -- Fetch order
    SELECT * INTO v_order FROM orders WHERE id = p_order_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Order % not found', p_order_id;
    END IF;
    v_user_id := v_order.user_id;

    -- Generate invoice number
    v_invoice_num := 'INV-' || nextval('gst_invoice_number_seq')::TEXT;

    -- Create invoice header
    INSERT INTO invoices (
        user_id, order_id, invoice_number, amount, tax,
        restaurant_id, branch_id, gstin, customer_gstin, place_of_supply,
        invoice_type, taxable_amount, cgst_amount, sgst_amount, igst_amount,
        discount_amount, total_amount, is_cancelled
    ) VALUES (
        v_user_id, p_order_id, v_invoice_num, v_order.total_amount, v_order.tax_amount,
        p_restaurant_id, p_branch_id, p_gstin, p_customer_gstin, p_place_of_supply,
        CASE WHEN p_customer_gstin IS NOT NULL THEN 'B2B' ELSE 'B2C' END,
        0, 0, 0, 0,
        v_order.discount_amount, v_order.total_amount, false
    )
    RETURNING id INTO v_invoice_id;

    -- Create line items with tax
    FOR v_item IN
        SELECT oi.*, i."Category" AS item_category
        FROM order_items oi
        LEFT JOIN items i ON i."Item_ID" = oi.item_id
        WHERE oi.order_id = p_order_id
    LOOP
        -- Resolve tax rate for this item
        SELECT * INTO v_tax_rate
        FROM tax_rates
        WHERE id = COALESCE(
            v_item.tax_rate_id,
            fn_resolve_tax_rate(p_restaurant_id, v_item.item_id, v_order.order_type,
                                v_order.platform, p_is_interstate, v_item.item_category)
        );

        IF v_tax_rate IS NULL THEN
            -- No tax applicable
            v_taxable := v_item.total_price;
            v_cgst := 0; v_sgst := 0; v_igst := 0;
        ELSE
            v_taxable := v_item.total_price - COALESCE(v_item.discount_amount, 0);
            IF v_tax_rate.is_inclusive THEN
                v_taxable := ROUND(v_taxable / (1 + v_tax_rate.rate_percentage / 100), 2);
            END IF;

            IF p_is_interstate THEN
                v_igst := ROUND(v_taxable * v_tax_rate.igst_percentage / 100, 2);
                v_cgst := 0; v_sgst := 0;
            ELSE
                v_cgst := ROUND(v_taxable * v_tax_rate.cgst_percentage / 100, 2);
                v_sgst := ROUND(v_taxable * v_tax_rate.sgst_percentage / 100, 2);
                v_igst := 0;
            END IF;
        END IF;

        -- Insert GST invoice item (IMMUTABLE snapshot)
        INSERT INTO gst_invoice_items (
            invoice_id, item_id, item_name, hsn_code,
            quantity, unit_price, discount, taxable_value,
            cgst_rate, cgst_amount, sgst_rate, sgst_amount,
            igst_rate, igst_amount, total_amount
        ) VALUES (
            v_invoice_id, v_item.item_id, COALESCE(v_item.item_name, 'Item'),
            COALESCE(v_tax_rate.hsn_code, ''),
            v_item.quantity, v_item.unit_price,
            COALESCE(v_item.discount_amount, 0), v_taxable,
            COALESCE(v_tax_rate.cgst_percentage, 0), v_cgst,
            COALESCE(v_tax_rate.sgst_percentage, 0), v_sgst,
            COALESCE(v_tax_rate.igst_percentage, 0), v_igst,
            v_taxable + v_cgst + v_sgst + v_igst
        );

        -- Also snapshot into order_tax_details (per-rate aggregation)
        INSERT INTO order_tax_details (
            order_id, tax_rate_id, tax_name, rate_percentage,
            taxable_amount, cgst_amount, sgst_amount, igst_amount, total_tax, is_inclusive
        ) VALUES (
            p_order_id,
            CASE WHEN v_tax_rate IS NOT NULL THEN v_tax_rate.id ELSE NULL END,
            COALESCE(v_tax_rate.name, 'No Tax'),
            COALESCE(v_tax_rate.rate_percentage, 0),
            v_taxable, v_cgst, v_sgst, v_igst, v_cgst + v_sgst + v_igst,
            COALESCE(v_tax_rate.is_inclusive, false)
        )
        ON CONFLICT DO NOTHING;

        v_total_taxable := v_total_taxable + v_taxable;
        v_total_cgst := v_total_cgst + v_cgst;
        v_total_sgst := v_total_sgst + v_sgst;
        v_total_igst := v_total_igst + v_igst;
    END LOOP;

    -- Update invoice header with totals
    UPDATE invoices SET
        taxable_amount = v_total_taxable,
        cgst_amount    = v_total_cgst,
        sgst_amount    = v_total_sgst,
        igst_amount    = v_total_igst,
        total_amount   = v_total_taxable + v_total_cgst + v_total_sgst + v_total_igst
                         + COALESCE(v_order.discount_amount, 0)
    WHERE id = v_invoice_id;

    -- Link invoice to order
    UPDATE orders SET invoice_id = v_invoice_id WHERE id = p_order_id;

    RETURN v_invoice_id;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 13: FUNCTION — Vendor Payment Journal
-- Creates journal: DR Accounts Payable, CR Cash/Bank
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_vendor_payment_journal(
    p_payment_id    UUID,
    p_restaurant_id UUID,
    p_branch_id     UUID DEFAULT NULL,
    p_created_by    TEXT DEFAULT 'system'
)
RETURNS UUID AS $$
DECLARE
    v_payment   RECORD;
    v_je_id     UUID;
    v_debit_acct UUID;
    v_credit_acct UUID;
    v_lines     JSONB;
BEGIN
    SELECT * INTO v_payment FROM vendor_payments WHERE id = p_payment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Vendor payment % not found', p_payment_id;
    END IF;

    -- DR Accounts Payable (2001)
    SELECT id INTO v_debit_acct FROM chart_of_accounts
    WHERE restaurant_id = p_restaurant_id AND account_code = '2001';

    -- CR Cash (1001) or Bank (1002)
    SELECT id INTO v_credit_acct FROM chart_of_accounts
    WHERE restaurant_id = p_restaurant_id
      AND account_code = CASE
          WHEN v_payment.payment_method IN ('cash') THEN '1001'
          ELSE '1002'
      END;

    IF v_debit_acct IS NULL OR v_credit_acct IS NULL THEN
        RETURN NULL;  -- Chart of accounts not seeded
    END IF;

    v_lines := jsonb_build_array(
        jsonb_build_object(
            'account_id', v_debit_acct::text,
            'debit', v_payment.amount,
            'credit', 0,
            'description', 'Accounts payable settled'
        ),
        jsonb_build_object(
            'account_id', v_credit_acct::text,
            'debit', 0,
            'credit', v_payment.amount,
            'description', 'Payment to vendor'
        )
    );

    SELECT fn_create_journal_entry(
        p_restaurant_id, p_branch_id, CURRENT_DATE,
        'vendor_payment', p_payment_id::text,
        'Vendor payment ' || p_payment_id::text,
        p_created_by, v_lines
    ) INTO v_je_id;

    -- Link journal entry to vendor_payment
    UPDATE vendor_payments SET journal_entry_id = v_je_id WHERE id = p_payment_id;

    RETURN v_je_id;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 14: FUNCTION — Consistency Validation
-- Call periodically or on-demand to verify ERP data integrity.
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_erp_consistency_check(p_restaurant_id UUID)
RETURNS TABLE (
    check_name      TEXT,
    status          TEXT,
    expected_value  NUMERIC,
    actual_value    NUMERIC,
    difference      NUMERIC
) AS $$
BEGIN
    -- Check 1: Inventory stock = ledger SUM
    RETURN QUERY
    SELECT
        'inventory_stock_vs_ledger'::TEXT AS check_name,
        CASE WHEN ABS(i.current_stock - COALESCE(l.net_stock, 0)) < 0.01
             THEN 'OK' ELSE 'MISMATCH' END AS status,
        COALESCE(l.net_stock, 0) AS expected_value,
        i.current_stock AS actual_value,
        i.current_stock - COALESCE(l.net_stock, 0) AS difference
    FROM ingredients i
    LEFT JOIN LATERAL (
        SELECT SUM(il.quantity_in) - SUM(il.quantity_out) AS net_stock
        FROM inventory_ledger il
        WHERE il.ingredient_id = i.id AND il.restaurant_id = p_restaurant_id
    ) l ON true
    WHERE i.restaurant_id = p_restaurant_id
      AND ABS(i.current_stock - COALESCE(l.net_stock, 0)) >= 0.01;

    -- Check 2: Journal entries balanced (debit = credit per entry)
    RETURN QUERY
    SELECT
        'journal_balance'::TEXT AS check_name,
        'IMBALANCED' AS status,
        SUM(jl.debit) AS expected_value,
        SUM(jl.credit) AS actual_value,
        SUM(jl.debit) - SUM(jl.credit) AS difference
    FROM journal_entries je
    JOIN journal_lines jl ON jl.journal_entry_id = je.id
    WHERE je.restaurant_id = p_restaurant_id
    GROUP BY je.id
    HAVING ABS(SUM(jl.debit) - SUM(jl.credit)) >= 0.01;

    -- Check 3: Order tax = order_tax_details sum
    RETURN QUERY
    SELECT
        'order_tax_vs_details'::TEXT AS check_name,
        CASE WHEN ABS(o.tax_amount - COALESCE(t.total_tax, 0)) < 0.01
             THEN 'OK' ELSE 'MISMATCH' END AS status,
        o.tax_amount AS expected_value,
        COALESCE(t.total_tax, 0) AS actual_value,
        o.tax_amount - COALESCE(t.total_tax, 0) AS difference
    FROM orders o
    LEFT JOIN LATERAL (
        SELECT SUM(otd.total_tax) AS total_tax
        FROM order_tax_details otd
        WHERE otd.order_id = o.id
    ) t ON true
    WHERE o.restaurant_id = p_restaurant_id
      AND o.status NOT IN ('cancelled', 'pending')
      AND ABS(o.tax_amount - COALESCE(t.total_tax, 0)) >= 0.01;
END;
$$ LANGUAGE plpgsql STABLE;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 15: FUNCTION — Aggregate Daily PnL (enhanced with platform split)
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_aggregate_daily_pnl_v2(
    p_restaurant_id UUID,
    p_branch_id     UUID,
    p_date          DATE DEFAULT CURRENT_DATE
)
RETURNS UUID AS $$
DECLARE
    v_pnl_id UUID;
    v_revenue NUMERIC(14,2);
    v_cogs    NUMERIC(14,2);
    v_tax     NUMERIC(14,2);
    v_orders  INTEGER;
    v_expenses NUMERIC(14,2);
    v_aov     NUMERIC(12,2);
BEGIN
    -- Revenue from completed orders
    SELECT
        COALESCE(SUM(total_amount), 0),
        COALESCE(SUM(cost_of_goods_sold), 0),
        COALESCE(SUM(tax_amount), 0),
        COUNT(*),
        CASE WHEN COUNT(*) > 0 THEN ROUND(SUM(total_amount) / COUNT(*), 2) ELSE 0 END
    INTO v_revenue, v_cogs, v_tax, v_orders, v_aov
    FROM orders
    WHERE restaurant_id = p_restaurant_id
      AND (p_branch_id IS NULL OR branch_id = p_branch_id)
      AND created_at::date = p_date
      AND status NOT IN ('cancelled', 'pending');

    -- Operating expenses from accounting_entries
    SELECT COALESCE(SUM(amount), 0) INTO v_expenses
    FROM accounting_entries
    WHERE restaurant_id_uuid = p_restaurant_id
      AND (p_branch_id IS NULL OR branch_id = p_branch_id)
      AND created_at::date = p_date
      AND entry_type = 'expense';

    -- Upsert
    INSERT INTO daily_pnl (
        restaurant_id, branch_id, pnl_date,
        total_revenue, total_cogs, gross_profit,
        operating_expenses, net_profit, tax_collected,
        total_orders, avg_order_value
    ) VALUES (
        p_restaurant_id, p_branch_id, p_date,
        v_revenue, v_cogs, v_revenue - v_cogs,
        v_expenses, v_revenue - v_cogs - v_expenses, v_tax,
        v_orders, v_aov
    )
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
        updated_at         = NOW()
    RETURNING id INTO v_pnl_id;

    RETURN v_pnl_id;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 16: SEED DEFAULT FEATURE FLAGS
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_seed_feature_flags(p_restaurant_id UUID)
RETURNS void AS $$
BEGIN
    INSERT INTO feature_flags (restaurant_id, flag_name, is_enabled, metadata) VALUES
        (p_restaurant_id, 'erp.auto_inventory_deduction',   true,  '{"description":"Auto-deduct inventory on order confirm"}'),
        (p_restaurant_id, 'erp.auto_journal_entries',       true,  '{"description":"Auto-create journal entries on payment"}'),
        (p_restaurant_id, 'erp.auto_gst_invoice',           true,  '{"description":"Auto-create GST invoice on order"}'),
        (p_restaurant_id, 'erp.tax_rule_engine',            false, '{"description":"Use dynamic tax rules instead of item_tax_mapping"}'),
        (p_restaurant_id, 'erp.platform_tax_handling',      false, '{"description":"Enable platform-specific GST handling"}'),
        (p_restaurant_id, 'erp.daily_pnl_auto_aggregate',   true,  '{"description":"Auto-aggregate daily P&L on shift close"}'),
        (p_restaurant_id, 'erp.e_invoice',                  false, '{"description":"Enable e-Invoice generation (IRN)"}')
    ON CONFLICT (restaurant_id, flag_name) DO NOTHING;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 17: VIEW — Order with full ERP context
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE VIEW v_order_erp_summary AS
SELECT
    o.id AS order_id,
    o.restaurant_id,
    o.branch_id,
    o.order_type,
    o.platform,
    o.subtotal,
    o.tax_amount,
    o.discount_amount,
    o.total_amount,
    o.cost_of_goods_sold,
    ROUND(o.total_amount - o.cost_of_goods_sold, 2) AS gross_profit,
    CASE WHEN o.total_amount > 0
         THEN ROUND((o.total_amount - o.cost_of_goods_sold) / o.total_amount * 100, 1)
         ELSE 0 END AS margin_percent,
    o.gst_handled_externally,
    o.status,
    o.created_at
FROM orders o;


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 18: IMMUTABILITY — Prevent edits to gst_invoice_items
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_prevent_gst_invoice_edit()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'gst_invoice_items are immutable. Create a credit note instead.';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prevent_gst_invoice_update ON gst_invoice_items;
CREATE TRIGGER trg_prevent_gst_invoice_update
    BEFORE UPDATE ON gst_invoice_items
    FOR EACH ROW
    EXECUTE FUNCTION fn_prevent_gst_invoice_edit();

DROP TRIGGER IF EXISTS trg_prevent_gst_invoice_delete ON gst_invoice_items;
CREATE TRIGGER trg_prevent_gst_invoice_delete
    BEFORE DELETE ON gst_invoice_items
    FOR EACH ROW
    EXECUTE FUNCTION fn_prevent_gst_invoice_edit();


-- ════════════════════════════════════════════════════════════════════════════
-- SECTION 19: ADDITIONAL INDEXES for connected queries
-- ════════════════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_order_items_tax
    ON order_items(tax_rate_id) WHERE tax_rate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_invoices_restaurant
    ON invoices(restaurant_id) WHERE restaurant_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_invoices_order
    ON invoices(order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ingredients_reorder
    ON ingredients(restaurant_id) WHERE reorder_level > 0;


-- ════════════════════════════════════════════════════════════════════════════
-- DONE. Non-breaking, backward-compatible.
-- ════════════════════════════════════════════════════════════════════════════

-- Run after migration:
--   SELECT fn_seed_feature_flags('your-restaurant-uuid');
