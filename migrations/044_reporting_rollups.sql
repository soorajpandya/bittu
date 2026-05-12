-- ============================================================================
-- Migration 044 — Phase 8: Reporting & Analytics
-- ----------------------------------------------------------------------------
-- Adds:
--   • merchant_daily_rollups   — one row per (merchant, date, currency)
--   • fn_compute_daily_rollup  — aggregates orders / payments / refunds /
--     disputes / merchant_ledger into the rollup table (UPSERT)
--   • reports.* permissions    — read / read.all / export / export.all
--
-- Hard rules respected:
--   • Read-only / aggregator only — no gateway wiring.
--   • Admin vs merchant scoping is enforced in the service/router layer; this
--     migration just creates the substrate.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS merchant_daily_rollups (
    id                          BIGSERIAL PRIMARY KEY,
    merchant_id                 UUID            NOT NULL,
    rollup_date                 DATE            NOT NULL,
    currency                    CHAR(3)         NOT NULL DEFAULT 'INR',

    -- order / sales side
    orders_count                INTEGER         NOT NULL DEFAULT 0,
    orders_completed_count      INTEGER         NOT NULL DEFAULT 0,
    gross_sales                 NUMERIC(18,4)   NOT NULL DEFAULT 0,
    discounts_total             NUMERIC(18,4)   NOT NULL DEFAULT 0,
    tax_total                   NUMERIC(18,4)   NOT NULL DEFAULT 0,
    cogs_total                  NUMERIC(18,4)   NOT NULL DEFAULT 0,

    -- payments
    payments_count              INTEGER         NOT NULL DEFAULT 0,
    payments_amount             NUMERIC(18,4)   NOT NULL DEFAULT 0,
    payments_cash_amount        NUMERIC(18,4)   NOT NULL DEFAULT 0,

    -- refunds
    refunds_count               INTEGER         NOT NULL DEFAULT 0,
    refunds_initiated_amount    NUMERIC(18,4)   NOT NULL DEFAULT 0,
    refunds_succeeded_amount    NUMERIC(18,4)   NOT NULL DEFAULT 0,
    refunds_failed_count        INTEGER         NOT NULL DEFAULT 0,

    -- disputes
    disputes_opened_count       INTEGER         NOT NULL DEFAULT 0,
    disputes_lost_amount        NUMERIC(18,4)   NOT NULL DEFAULT 0,
    disputes_won_count          INTEGER         NOT NULL DEFAULT 0,

    -- ledger rollup (Phase 1 merchant_ledger)
    ledger_debit                NUMERIC(18,4)   NOT NULL DEFAULT 0,
    ledger_credit               NUMERIC(18,4)   NOT NULL DEFAULT 0,
    ledger_net                  NUMERIC(18,4)   NOT NULL DEFAULT 0,
    fees_total                  NUMERIC(18,4)   NOT NULL DEFAULT 0,
    gst_total                   NUMERIC(18,4)   NOT NULL DEFAULT 0,
    chargebacks_total           NUMERIC(18,4)   NOT NULL DEFAULT 0,

    computed_at                 TIMESTAMPTZ     NOT NULL DEFAULT now(),
    computed_by                 UUID,
    source_version              INTEGER         NOT NULL DEFAULT 1,

    CONSTRAINT uq_merchant_daily_rollups
        UNIQUE (merchant_id, rollup_date, currency)
);

CREATE INDEX IF NOT EXISTS ix_merchant_daily_rollups_merchant_date
    ON merchant_daily_rollups (merchant_id, rollup_date DESC);
CREATE INDEX IF NOT EXISTS ix_merchant_daily_rollups_date
    ON merchant_daily_rollups (rollup_date DESC);

-- ----------------------------------------------------------------------------
-- fn_compute_daily_rollup
--   Aggregates a single (merchant, date, currency) day from source tables and
--   UPSERTs into merchant_daily_rollups. Returns the resulting row as JSONB.
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_compute_daily_rollup(
    p_merchant_id  UUID,
    p_date         DATE,
    p_currency     CHAR(3) DEFAULT 'INR',
    p_computed_by  UUID    DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
AS $$
DECLARE
    v_day_start TIMESTAMPTZ := p_date::timestamptz;
    v_day_end   TIMESTAMPTZ := (p_date + INTERVAL '1 day')::timestamptz;

    v_orders_count               INT     := 0;
    v_orders_completed_count     INT     := 0;
    v_gross_sales                NUMERIC := 0;
    v_discounts_total            NUMERIC := 0;
    v_tax_total                  NUMERIC := 0;
    v_cogs_total                 NUMERIC := 0;

    v_payments_count             INT     := 0;
    v_payments_amount            NUMERIC := 0;
    v_payments_cash_amount       NUMERIC := 0;

    v_refunds_count              INT     := 0;
    v_refunds_initiated_amount   NUMERIC := 0;
    v_refunds_succeeded_amount   NUMERIC := 0;
    v_refunds_failed_count       INT     := 0;

    v_disputes_opened_count      INT     := 0;
    v_disputes_lost_amount       NUMERIC := 0;
    v_disputes_won_count         INT     := 0;

    v_ledger_debit               NUMERIC := 0;
    v_ledger_credit              NUMERIC := 0;
    v_ledger_net                 NUMERIC := 0;
    v_fees_total                 NUMERIC := 0;
    v_gst_total                  NUMERIC := 0;
    v_chargebacks_total          NUMERIC := 0;

    v_row merchant_daily_rollups;
BEGIN
    -- ─── orders
    SELECT
        COUNT(*),
        COUNT(*) FILTER (WHERE status = 'completed'),
        COALESCE(SUM(total_amount), 0),
        COALESCE(SUM(discount_amount), 0),
        COALESCE(SUM(tax_amount), 0),
        COALESCE(SUM(cost_of_goods_sold), 0)
    INTO v_orders_count, v_orders_completed_count,
         v_gross_sales, v_discounts_total, v_tax_total, v_cogs_total
    FROM orders
    WHERE restaurant_id = p_merchant_id
      AND created_at >= v_day_start
      AND created_at <  v_day_end;

    -- ─── payments
    SELECT
        COUNT(*),
        COALESCE(SUM(amount), 0),
        COALESCE(SUM(amount) FILTER (WHERE method = 'cash'), 0)
    INTO v_payments_count, v_payments_amount, v_payments_cash_amount
    FROM payments
    WHERE restaurant_id = p_merchant_id
      AND status        = 'completed'
      AND created_at   >= v_day_start
      AND created_at   <  v_day_end;

    -- ─── refunds
    SELECT
        COUNT(*),
        COALESCE(SUM(amount), 0),
        COALESCE(SUM(amount) FILTER (WHERE status = 'succeeded'), 0),
        COUNT(*) FILTER (WHERE status = 'failed')
    INTO v_refunds_count, v_refunds_initiated_amount,
         v_refunds_succeeded_amount, v_refunds_failed_count
    FROM refunds
    WHERE merchant_id = p_merchant_id
      AND currency    = p_currency
      AND created_at >= v_day_start
      AND created_at <  v_day_end;

    -- ─── disputes
    SELECT
        COUNT(*),
        COALESCE(SUM(amount) FILTER (WHERE status = 'lost'), 0),
        COUNT(*) FILTER (WHERE status = 'won')
    INTO v_disputes_opened_count, v_disputes_lost_amount, v_disputes_won_count
    FROM disputes
    WHERE merchant_id = p_merchant_id
      AND currency    = p_currency
      AND opened_at  >= v_day_start
      AND opened_at  <  v_day_end;

    -- ─── merchant_ledger
    SELECT
        COALESCE(SUM(debit_amount), 0),
        COALESCE(SUM(credit_amount), 0),
        COALESCE(SUM(credit_amount - debit_amount), 0),
        COALESCE(SUM(debit_amount) FILTER (WHERE transaction_type = 'fee_deduction'), 0),
        COALESCE(SUM(debit_amount) FILTER (WHERE transaction_type = 'gst_deduction'), 0),
        COALESCE(SUM(debit_amount) FILTER (WHERE transaction_type = 'chargeback'), 0)
    INTO v_ledger_debit, v_ledger_credit, v_ledger_net,
         v_fees_total, v_gst_total, v_chargebacks_total
    FROM merchant_ledger
    WHERE merchant_id = p_merchant_id
      AND currency    = p_currency
      AND created_at >= v_day_start
      AND created_at <  v_day_end;

    -- ─── UPSERT
    INSERT INTO merchant_daily_rollups (
        merchant_id, rollup_date, currency,
        orders_count, orders_completed_count,
        gross_sales, discounts_total, tax_total, cogs_total,
        payments_count, payments_amount, payments_cash_amount,
        refunds_count, refunds_initiated_amount,
        refunds_succeeded_amount, refunds_failed_count,
        disputes_opened_count, disputes_lost_amount, disputes_won_count,
        ledger_debit, ledger_credit, ledger_net,
        fees_total, gst_total, chargebacks_total,
        computed_at, computed_by, source_version
    ) VALUES (
        p_merchant_id, p_date, p_currency,
        v_orders_count, v_orders_completed_count,
        v_gross_sales, v_discounts_total, v_tax_total, v_cogs_total,
        v_payments_count, v_payments_amount, v_payments_cash_amount,
        v_refunds_count, v_refunds_initiated_amount,
        v_refunds_succeeded_amount, v_refunds_failed_count,
        v_disputes_opened_count, v_disputes_lost_amount, v_disputes_won_count,
        v_ledger_debit, v_ledger_credit, v_ledger_net,
        v_fees_total, v_gst_total, v_chargebacks_total,
        now(), p_computed_by, 1
    )
    ON CONFLICT (merchant_id, rollup_date, currency) DO UPDATE
    SET orders_count               = EXCLUDED.orders_count,
        orders_completed_count     = EXCLUDED.orders_completed_count,
        gross_sales                = EXCLUDED.gross_sales,
        discounts_total            = EXCLUDED.discounts_total,
        tax_total                  = EXCLUDED.tax_total,
        cogs_total                 = EXCLUDED.cogs_total,
        payments_count             = EXCLUDED.payments_count,
        payments_amount            = EXCLUDED.payments_amount,
        payments_cash_amount       = EXCLUDED.payments_cash_amount,
        refunds_count              = EXCLUDED.refunds_count,
        refunds_initiated_amount   = EXCLUDED.refunds_initiated_amount,
        refunds_succeeded_amount   = EXCLUDED.refunds_succeeded_amount,
        refunds_failed_count       = EXCLUDED.refunds_failed_count,
        disputes_opened_count      = EXCLUDED.disputes_opened_count,
        disputes_lost_amount       = EXCLUDED.disputes_lost_amount,
        disputes_won_count         = EXCLUDED.disputes_won_count,
        ledger_debit               = EXCLUDED.ledger_debit,
        ledger_credit              = EXCLUDED.ledger_credit,
        ledger_net                 = EXCLUDED.ledger_net,
        fees_total                 = EXCLUDED.fees_total,
        gst_total                  = EXCLUDED.gst_total,
        chargebacks_total          = EXCLUDED.chargebacks_total,
        computed_at                = now(),
        computed_by                = EXCLUDED.computed_by,
        source_version             = merchant_daily_rollups.source_version + 1
    RETURNING * INTO v_row;

    RETURN to_jsonb(v_row);
END;
$$;

-- ----------------------------------------------------------------------------
-- Permissions
-- ----------------------------------------------------------------------------
INSERT INTO permissions (key) VALUES
    ('reports.read'),
    ('reports.read.all'),
    ('reports.export'),
    ('reports.export.all')
ON CONFLICT (key) DO NOTHING;

-- Owner + manager get the non-".all" set.
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
FROM   roles       r
JOIN   permissions p
       ON p.key IN ('reports.read', 'reports.export')
WHERE  r.name IN ('owner', 'manager')
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
