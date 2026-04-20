-- ════════════════════════════════════════════════════════════════════════════
-- Migration 025: Financial Product Layer
--
-- Transforms the Financial API Platform → Financial Operating System
--
-- 1. daily_closings table — tracks daily close workflow state
-- 2. gst_filing_workflows — tracks GST filing pipeline
-- 3. recon_workflows — tracks reconciliation workflow state
-- 4. Actionable alerts — suggested_action, auto_resolve_rule columns
-- 5. Granular finance permissions
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. DAILY CLOSINGS TABLE
--
--    Tracks the daily close workflow:
--    open → cash_counted → reviewed → closed
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS daily_closings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id UUID NOT NULL,
    branch_id UUID,
    closing_date DATE NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'open',   -- open, cash_counted, reviewed, closed
    -- System-computed from ledger
    expected_cash NUMERIC(14,2) NOT NULL DEFAULT 0,
    expected_card NUMERIC(14,2) NOT NULL DEFAULT 0,
    expected_upi NUMERIC(14,2) NOT NULL DEFAULT 0,
    -- User-entered actuals
    actual_cash NUMERIC(14,2),
    actual_card NUMERIC(14,2),
    actual_upi NUMERIC(14,2),
    -- Computed differences
    cash_difference NUMERIC(14,2),
    card_difference NUMERIC(14,2),
    upi_difference NUMERIC(14,2),
    -- Metadata
    total_orders INT NOT NULL DEFAULT 0,
    total_revenue NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_refunds NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_discounts NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_expenses NUMERIC(14,2) NOT NULL DEFAULT 0,
    net_revenue NUMERIC(14,2) NOT NULL DEFAULT 0,
    -- Workflow
    notes TEXT,
    counted_by TEXT,
    counted_at TIMESTAMPTZ,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    closed_by TEXT,
    closed_at TIMESTAMPTZ,
    period_locked BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(restaurant_id, branch_id, closing_date)
);

CREATE INDEX IF NOT EXISTS idx_daily_closings_lookup
    ON daily_closings(restaurant_id, closing_date DESC);

CREATE INDEX IF NOT EXISTS idx_daily_closings_branch
    ON daily_closings(restaurant_id, branch_id, closing_date DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- 2. GST FILING WORKFLOWS
--
--    Pipeline: draft → generated → reviewed → exported → filed → paid
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS gst_filing_workflows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id UUID NOT NULL,
    period_start DATE NOT NULL,
    period_end DATE NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',  -- draft, generated, reviewed, exported, filed, paid
    -- Computed GST data
    cgst_collected NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst_collected NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst_collected NUMERIC(14,2) NOT NULL DEFAULT 0,
    cgst_input NUMERIC(14,2) NOT NULL DEFAULT 0,
    sgst_input NUMERIC(14,2) NOT NULL DEFAULT 0,
    igst_input NUMERIC(14,2) NOT NULL DEFAULT 0,
    net_payable NUMERIC(14,2) NOT NULL DEFAULT 0,
    -- Tracking
    generated_at TIMESTAMPTZ,
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    exported_at TIMESTAMPTZ,
    filed_at TIMESTAMPTZ,
    filed_reference TEXT,     -- GST portal reference number
    paid_at TIMESTAMPTZ,
    paid_amount NUMERIC(14,2),
    paid_reference TEXT,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(restaurant_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_gst_filing_lookup
    ON gst_filing_workflows(restaurant_id, status);


-- ════════════════════════════════════════════════════════════════════════════
-- 3. ENHANCE FINANCIAL ALERTS — add actionability
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE financial_alerts
    ADD COLUMN IF NOT EXISTS suggested_action TEXT,
    ADD COLUMN IF NOT EXISTS auto_resolve_rule VARCHAR(64),
    ADD COLUMN IF NOT EXISTS notified BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS resolution_notes TEXT;


-- ════════════════════════════════════════════════════════════════════════════
-- 4. ENHANCED ALERT SCANNER — now with suggested actions
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_scan_financial_alerts(p_restaurant_id UUID)
RETURNS INT AS $$
DECLARE
    v_alert_count INT := 0;
    v_cash_balance NUMERIC;
    v_total_debit NUMERIC;
    v_total_credit NUMERIC;
    v_cogs NUMERIC;
    v_sales NUMERIC;
BEGIN
    -- Clear old unresolved alerts of same type before re-scanning
    DELETE FROM financial_alerts
    WHERE restaurant_id = p_restaurant_id
      AND is_resolved = false
      AND created_at < NOW() - INTERVAL '1 hour';

    -- Alert 1: Negative cash balance
    SELECT COALESCE(SUM(jl.debit - jl.credit), 0)
    INTO v_cash_balance
    FROM journal_lines jl
    JOIN journal_entries je ON je.id = jl.journal_entry_id
    JOIN chart_of_accounts coa ON coa.id = jl.account_id
    WHERE je.restaurant_id = p_restaurant_id
      AND je.is_reversed = false
      AND coa.system_code = 'CASH_ACCOUNT';

    IF v_cash_balance < 0 THEN
        INSERT INTO financial_alerts
            (restaurant_id, alert_type, severity, title, details, suggested_action)
        VALUES (p_restaurant_id, 'negative_cash', 'error',
                'Negative cash balance: ₹' || ROUND(ABS(v_cash_balance), 2),
                jsonb_build_object('cash_balance', v_cash_balance),
                'Record a cash top-up or check for missing sale entries')
        ON CONFLICT DO NOTHING;
        v_alert_count := v_alert_count + 1;
    END IF;

    -- Alert 2: Trial balance imbalance
    SELECT COALESCE(SUM(jl.debit), 0), COALESCE(SUM(jl.credit), 0)
    INTO v_total_debit, v_total_credit
    FROM journal_lines jl
    JOIN journal_entries je ON je.id = jl.journal_entry_id
    WHERE je.restaurant_id = p_restaurant_id;

    IF ABS(v_total_debit - v_total_credit) > 0.01 THEN
        INSERT INTO financial_alerts
            (restaurant_id, alert_type, severity, title, details, suggested_action)
        VALUES (p_restaurant_id, 'imbalance', 'error',
                'Trial balance mismatch: ₹' || ROUND(ABS(v_total_debit - v_total_credit), 2),
                jsonb_build_object('total_debit', v_total_debit, 'total_credit', v_total_credit,
                                   'difference', v_total_debit - v_total_credit),
                'Run integrity check and contact support — this should not happen in normal operation')
        ON CONFLICT DO NOTHING;
        v_alert_count := v_alert_count + 1;
    END IF;

    -- Alert 3: COGS exceeds sales (current month)
    SELECT COALESCE(SUM(jl.debit), 0)
    INTO v_cogs
    FROM journal_lines jl
    JOIN journal_entries je ON je.id = jl.journal_entry_id
    JOIN chart_of_accounts coa ON coa.id = jl.account_id
    WHERE je.restaurant_id = p_restaurant_id
      AND je.is_reversed = false
      AND coa.system_code IN ('COGS_FOOD', 'COGS_BEVERAGE')
      AND je.entry_date >= date_trunc('month', CURRENT_DATE);

    SELECT COALESCE(SUM(jl.credit), 0)
    INTO v_sales
    FROM journal_lines jl
    JOIN journal_entries je ON je.id = jl.journal_entry_id
    JOIN chart_of_accounts coa ON coa.id = jl.account_id
    WHERE je.restaurant_id = p_restaurant_id
      AND je.is_reversed = false
      AND coa.system_code = 'SALES_REVENUE'
      AND je.entry_date >= date_trunc('month', CURRENT_DATE);

    IF v_cogs > v_sales AND v_sales > 0 THEN
        INSERT INTO financial_alerts
            (restaurant_id, alert_type, severity, title, details, suggested_action)
        VALUES (p_restaurant_id, 'cogs_exceeds_sales', 'warning',
                'Food cost ' || ROUND(v_cogs / NULLIF(v_sales, 0) * 100, 1) || '% — exceeds revenue',
                jsonb_build_object('cogs', v_cogs, 'sales', v_sales,
                                   'ratio', ROUND(v_cogs / NULLIF(v_sales, 0) * 100, 1)),
                'Review purchase prices and portion sizes. Check for inventory waste or theft')
        ON CONFLICT DO NOTHING;
        v_alert_count := v_alert_count + 1;
    END IF;

    -- Alert 4: Unclosed previous day
    IF NOT EXISTS (
        SELECT 1 FROM daily_closings
        WHERE restaurant_id = p_restaurant_id
          AND closing_date = CURRENT_DATE - INTERVAL '1 day'
          AND status = 'closed'
    ) AND EXISTS (
        SELECT 1 FROM journal_entries
        WHERE restaurant_id = p_restaurant_id
          AND entry_date = CURRENT_DATE - INTERVAL '1 day'
    ) THEN
        INSERT INTO financial_alerts
            (restaurant_id, alert_type, severity, title, details, suggested_action)
        VALUES (p_restaurant_id, 'unclosed_day', 'warning',
                'Yesterday was not closed — cash verification pending',
                jsonb_build_object('date', (CURRENT_DATE - INTERVAL '1 day')::date),
                'Go to Daily Closing and close yesterday before proceeding')
        ON CONFLICT DO NOTHING;
        v_alert_count := v_alert_count + 1;
    END IF;

    -- Alert 5: High unreconciled bank transactions
    DECLARE v_unrec INT;
    BEGIN
        SELECT COUNT(*) INTO v_unrec
        FROM bank_statements
        WHERE restaurant_id = p_restaurant_id
          AND reconciled = false AND excluded = false;

        IF v_unrec > 20 THEN
            INSERT INTO financial_alerts
                (restaurant_id, alert_type, severity, title, details, suggested_action)
            VALUES (p_restaurant_id, 'high_unreconciled', 'warning',
                    v_unrec || ' unreconciled bank transactions',
                    jsonb_build_object('count', v_unrec),
                    'Import latest bank statement and run auto-match')
            ON CONFLICT DO NOTHING;
            v_alert_count := v_alert_count + 1;
        END IF;
    END;

    RETURN v_alert_count;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- 5. GRANULAR FINANCE PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    ('finance.pnl'),
    ('finance.cash'),
    ('finance.gst'),
    ('finance.expenses'),
    ('finance.recon'),
    ('finance.invoices'),
    ('finance.daily_close'),
    ('finance.trust_status')
ON CONFLICT (key) DO NOTHING;

-- Owner + Manager get all
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.name IN ('owner', 'manager')
  AND p.key IN ('finance.pnl', 'finance.cash', 'finance.gst', 'finance.expenses',
                'finance.recon', 'finance.invoices', 'finance.daily_close', 'finance.trust_status')
ON CONFLICT DO NOTHING;

-- Cashier gets cash + daily close + trust status
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.name = 'cashier'
  AND p.key IN ('finance.cash', 'finance.daily_close', 'finance.trust_status')
ON CONFLICT DO NOTHING;

COMMIT;
