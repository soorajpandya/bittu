-- ════════════════════════════════════════════════════════════════════════════
-- Migration 024: Financial Operating System
--
-- 1. Materialized views for dashboard performance
-- 2. Financial alerts table
-- 3. Audit trail enhancement (old_value / new_value)
-- 4. Finance permissions (finance.dashboard, finance.report, finance.audit)
-- 5. Branch-aware indexes for report filtering
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. MATERIALIZED VIEW — Daily Revenue Snapshot
--
--    Pre-aggregated daily totals per restaurant + branch.
--    Refreshed on demand; dashboards read from here.
-- ════════════════════════════════════════════════════════════════════════════

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_daily_revenue AS
SELECT
    je.restaurant_id,
    je.branch_id,
    je.entry_date,
    je.reference_type,
    COALESCE(SUM(jl.debit),  0) AS total_debit,
    COALESCE(SUM(jl.credit), 0) AS total_credit,
    COUNT(DISTINCT je.id)        AS entry_count
FROM journal_entries je
JOIN journal_lines jl ON jl.journal_entry_id = je.id
WHERE je.is_reversed = false
GROUP BY je.restaurant_id, je.branch_id, je.entry_date, je.reference_type;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_daily_revenue_pk
    ON mv_daily_revenue(restaurant_id, COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid), entry_date, reference_type);

CREATE INDEX IF NOT EXISTS idx_mv_daily_revenue_date
    ON mv_daily_revenue(restaurant_id, entry_date);

-- ════════════════════════════════════════════════════════════════════════════
-- 2. MATERIALIZED VIEW — Account Balances Snapshot
--
--    Running balance per account per restaurant. Used by dashboard and
--    quick-balance lookups without scanning all journal_lines.
-- ════════════════════════════════════════════════════════════════════════════

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_account_balances AS
SELECT
    coa.restaurant_id,
    coa.id          AS account_id,
    coa.account_code,
    coa.name        AS account_name,
    coa.system_code,
    coa.account_type,
    COALESCE(SUM(jl.debit),  0) AS total_debit,
    COALESCE(SUM(jl.credit), 0) AS total_credit,
    COALESCE(SUM(jl.debit) - SUM(jl.credit), 0) AS net_balance
FROM chart_of_accounts coa
LEFT JOIN journal_lines jl ON jl.account_id = coa.id
LEFT JOIN journal_entries je ON je.id = jl.journal_entry_id
    AND je.is_reversed = false
WHERE coa.is_active = true
GROUP BY coa.restaurant_id, coa.id, coa.account_code, coa.name,
         coa.system_code, coa.account_type;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_account_balances_pk
    ON mv_account_balances(restaurant_id, account_id);

CREATE INDEX IF NOT EXISTS idx_mv_account_balances_type
    ON mv_account_balances(restaurant_id, account_type);

CREATE INDEX IF NOT EXISTS idx_mv_account_balances_syscode
    ON mv_account_balances(restaurant_id, system_code);


-- ════════════════════════════════════════════════════════════════════════════
-- 3. FINANCIAL ALERTS TABLE
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS financial_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id UUID NOT NULL,
    branch_id UUID,
    alert_type VARCHAR(64) NOT NULL,       -- 'negative_cash', 'imbalance', 'cogs_exceeds_sales', 'period_gap', 'orphan_entry'
    severity VARCHAR(16) NOT NULL DEFAULT 'warning',  -- 'error', 'warning', 'info'
    title VARCHAR(256) NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_resolved BOOLEAN NOT NULL DEFAULT false,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_financial_alerts_restaurant
    ON financial_alerts(restaurant_id, is_resolved, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_financial_alerts_type
    ON financial_alerts(restaurant_id, alert_type);


-- ════════════════════════════════════════════════════════════════════════════
-- 4. FINANCIAL AUDIT LOG (enhanced — tracks old/new values for financial ops)
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS financial_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id UUID NOT NULL,
    user_id TEXT NOT NULL,
    action VARCHAR(128) NOT NULL,          -- 'period.close', 'period.reopen', 'invoice.void', 'expense.approve', 'recon.match', 'recon.unmatch'
    entity_type VARCHAR(64) NOT NULL,      -- 'accounting_period', 'invoice', 'expense', 'bank_reconciliation', 'journal_entry'
    entity_id TEXT,
    old_value JSONB,
    new_value JSONB,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ip_address VARCHAR(45),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_financial_audit_restaurant
    ON financial_audit_log(restaurant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_financial_audit_entity
    ON financial_audit_log(restaurant_id, entity_type, entity_id);

CREATE INDEX IF NOT EXISTS idx_financial_audit_action
    ON financial_audit_log(restaurant_id, action);


-- ════════════════════════════════════════════════════════════════════════════
-- 5. FUNCTION — Refresh materialized views
-- ════════════════════════════════════════════════════════════════════════════

CREATE OR REPLACE FUNCTION fn_refresh_financial_views()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_daily_revenue;
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_account_balances;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- 6. FUNCTION — Run financial alerts scan
--
--    Checks for anomalies and inserts alerts.
--    Meant to be called periodically or on-demand.
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
    v_period_gap_count INT;
BEGIN
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
        INSERT INTO financial_alerts (restaurant_id, alert_type, severity, title, details)
        VALUES (p_restaurant_id, 'negative_cash', 'error',
                'Negative cash balance detected',
                jsonb_build_object('cash_balance', v_cash_balance))
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
        INSERT INTO financial_alerts (restaurant_id, alert_type, severity, title, details)
        VALUES (p_restaurant_id, 'imbalance', 'error',
                'Trial balance is not balanced',
                jsonb_build_object('total_debit', v_total_debit, 'total_credit', v_total_credit,
                                   'difference', v_total_debit - v_total_credit))
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
        INSERT INTO financial_alerts (restaurant_id, alert_type, severity, title, details)
        VALUES (p_restaurant_id, 'cogs_exceeds_sales', 'warning',
                'COGS exceeds sales revenue this month',
                jsonb_build_object('cogs', v_cogs, 'sales', v_sales,
                                   'ratio', ROUND(v_cogs / NULLIF(v_sales, 0) * 100, 1)))
        ON CONFLICT DO NOTHING;
        v_alert_count := v_alert_count + 1;
    END IF;

    RETURN v_alert_count;
END;
$$ LANGUAGE plpgsql;


-- ════════════════════════════════════════════════════════════════════════════
-- 7. BRANCH-AWARE INDEXES (accelerate branch-filtered reports)
-- ════════════════════════════════════════════════════════════════════════════

CREATE INDEX IF NOT EXISTS idx_journal_entries_branch
    ON journal_entries(restaurant_id, branch_id, entry_date);

CREATE INDEX IF NOT EXISTS idx_journal_entries_ref_type_date
    ON journal_entries(restaurant_id, reference_type, entry_date)
    WHERE is_reversed = false;


-- ════════════════════════════════════════════════════════════════════════════
-- 8. FINANCE PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    ('finance.dashboard'),
    ('finance.report'),
    ('finance.audit')
ON CONFLICT (key) DO NOTHING;

-- Grant to owner and manager
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.name IN ('owner', 'manager')
  AND p.key IN ('finance.dashboard', 'finance.report', 'finance.audit')
ON CONFLICT DO NOTHING;

-- Grant dashboard read to cashier
INSERT INTO role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM roles r
CROSS JOIN permissions p
WHERE r.name = 'cashier'
  AND p.key = 'finance.dashboard'
ON CONFLICT DO NOTHING;

COMMIT;
