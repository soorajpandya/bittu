-- ============================================================================
-- Migration 020: Accounting Rules Engine + Payment Gateway Settlement
--
-- SAFE: All operations are ADDITIVE only.
--   - Adds accounting_rules table (configurable event→journal mappings)
--   - Adds pg_settlements table (track gateway settlement batches)
--   - Adds new CoA accounts: PG Clearing, Gateway Charges
--   - Adds permissions for settlement and rule management
--   - Does NOT modify or remove any existing data/logic
-- ============================================================================
BEGIN;

-- ════════════════════════════════════════════════════════════════════════════
-- 1. ACCOUNTING RULES — Configurable event-to-journal mappings
--    Replaces hardcoded DR/CR patterns with per-restaurant overrides.
--    Engine falls back to built-in defaults if no custom rules exist.
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS accounting_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    event_type      VARCHAR(100) NOT NULL,
    -- e.g. 'PAYMENT_COMPLETED', 'ORDER_CONFIRMED', 'GRN_VERIFIED', ...

    rule_name       VARCHAR(200) NOT NULL,
    description     TEXT,

    -- Journal line templates (each rule produces one debit + one credit line)
    debit_account_code  VARCHAR(20) NOT NULL,
    credit_account_code VARCHAR(20) NOT NULL,

    -- Amount source: which field from the event payload to use
    -- e.g. 'amount', 'total_amount', 'tax_amount', 'cogs_amount'
    amount_field    VARCHAR(100) NOT NULL DEFAULT 'amount',

    -- Optional multiplier (e.g. 0.5 for splitting, -1 for reversal)
    amount_multiplier NUMERIC(10,4) NOT NULL DEFAULT 1.0,

    -- Conditions (JSON) — rule only fires if ALL conditions match the event payload
    -- Example: {"method": "cash"} or {"method": ["upi", "card"]} or {"platform": "zomato"}
    conditions      JSONB NOT NULL DEFAULT '{}',

    -- Priority: higher = evaluated first. First matching rule wins per event_type group.
    priority        INTEGER NOT NULL DEFAULT 100,

    -- Whether this rule is active
    is_active       BOOLEAN NOT NULL DEFAULT true,

    -- Reference type for the journal entry (overrides default)
    reference_type_override VARCHAR(50),

    -- Description template for journal entry (supports {field} placeholders)
    description_template VARCHAR(500),

    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ar_restaurant_event
    ON accounting_rules(restaurant_id, event_type, is_active, priority DESC);

-- Allow multiple rules per event (e.g. one for cash, one for online)
-- but enforce uniqueness of rule name per restaurant
CREATE UNIQUE INDEX IF NOT EXISTS idx_ar_unique_name
    ON accounting_rules(restaurant_id, rule_name) WHERE is_active = true;


-- ════════════════════════════════════════════════════════════════════════════
-- 2. PAYMENT GATEWAY SETTLEMENTS — Track money arriving from Razorpay/Cashfree
--
--    Lifecycle:
--      Payment success → DR PG Clearing, CR Accounts Receivable
--      Settlement received → DR Bank, CR PG Clearing
--      Settlement fees → DR Gateway Charges, CR PG Clearing
-- ════════════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS pg_settlements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id),

    -- Gateway info
    gateway         VARCHAR(50) NOT NULL,  -- 'razorpay', 'cashfree', 'phonepe'
    settlement_id   VARCHAR(200),          -- gateway's settlement/payout ID
    settlement_date DATE NOT NULL,

    -- Amounts
    gross_amount    NUMERIC(14,2) NOT NULL,  -- total captured
    gateway_fee     NUMERIC(14,2) NOT NULL DEFAULT 0,
    tax_on_fee      NUMERIC(14,2) NOT NULL DEFAULT 0,  -- GST on gateway fee
    net_amount      NUMERIC(14,2) NOT NULL,  -- actually deposited = gross - fee - tax

    -- Status
    status          VARCHAR(30) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'received', 'reconciled', 'disputed')),

    -- Linked payments (array of payment IDs included in this settlement)
    payment_ids     UUID[] DEFAULT '{}',

    -- Journal entry references
    clearing_journal_id  UUID,  -- journal when payment captured (DR Clearing, CR AR)
    settlement_journal_id UUID, -- journal when settlement received (DR Bank, CR Clearing)
    fee_journal_id       UUID,  -- journal for gateway fees (DR Charges, CR Clearing)

    -- Reconciliation
    reconciled_by   TEXT,
    reconciled_at   TIMESTAMPTZ,
    notes           TEXT,

    created_by      TEXT NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pgs_restaurant_status
    ON pg_settlements(restaurant_id, status);

CREATE INDEX IF NOT EXISTS idx_pgs_restaurant_date
    ON pg_settlements(restaurant_id, settlement_date DESC);

CREATE INDEX IF NOT EXISTS idx_pgs_settlement_id
    ON pg_settlements(gateway, settlement_id) WHERE settlement_id IS NOT NULL;


-- ════════════════════════════════════════════════════════════════════════════
-- 3. NEW CHART OF ACCOUNTS — PG Clearing + Gateway Charges
-- ════════════════════════════════════════════════════════════════════════════

-- Add new system accounts to existing restaurants' CoA
-- PG Clearing = current asset (money in transit from gateway)
-- Gateway Charges = expense (fees deducted by Razorpay/Cashfree)
-- Tax on Gateway Fees = expense (GST on gateway charges)

DO $$
DECLARE
    r RECORD;
    v_parent_asset UUID;
    v_parent_expense UUID;
BEGIN
    FOR r IN SELECT id FROM restaurants LOOP
        -- Find parent asset account (1000)
        SELECT id INTO v_parent_asset
        FROM chart_of_accounts
        WHERE restaurant_id = r.id AND account_code = '1000' LIMIT 1;

        -- Find parent expense account (5000 or first expense)
        SELECT id INTO v_parent_expense
        FROM chart_of_accounts
        WHERE restaurant_id = r.id AND account_type = 'expense' AND parent_id IS NULL
        ORDER BY account_code LIMIT 1;

        -- PG Clearing Account (asset — money in transit)
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '1006', 'Payment Gateway Clearing', 'asset', v_parent_asset,
             'PG_CLEARING', true, true,
             'Money captured by payment gateway but not yet settled to bank')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'PG_CLEARING',
                name = 'Payment Gateway Clearing',
                description = 'Money captured by payment gateway but not yet settled to bank';

        -- Gateway Charges (expense)
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5011', 'Payment Gateway Charges', 'expense', v_parent_expense,
             'GATEWAY_CHARGES', true, true,
             'Transaction fees charged by Razorpay, Cashfree, PhonePe etc.')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'GATEWAY_CHARGES',
                name = 'Payment Gateway Charges',
                description = 'Transaction fees charged by Razorpay, Cashfree, PhonePe etc.';

        -- GST on Gateway Fees (expense)
        INSERT INTO chart_of_accounts
            (restaurant_id, account_code, name, account_type, parent_id,
             system_code, is_system, is_active, description)
        VALUES
            (r.id, '5012', 'Tax on Gateway Charges', 'expense', v_parent_expense,
             'GATEWAY_TAX', true, true,
             'GST charged on payment gateway transaction fees')
        ON CONFLICT (restaurant_id, account_code) DO UPDATE
            SET system_code = 'GATEWAY_TAX',
                name = 'Tax on Gateway Charges',
                description = 'GST charged on payment gateway transaction fees';

    END LOOP;
END $$;


-- ════════════════════════════════════════════════════════════════════════════
-- 4. PERMISSIONS
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO permissions (key) VALUES
    ('settlement.read'),
    ('settlement.write'),
    ('settlement.reconcile'),
    ('accounting.rules.read'),
    ('accounting.rules.write')
ON CONFLICT (key) DO NOTHING;

-- Settlement: owner + manager can read/write; only owner can reconcile
-- Rules: owner only (financial config)
WITH role_perm(role_name, perm_key, allowed, meta) AS (
    VALUES
    ('owner',   'settlement.read',          true, '{}'::jsonb),
    ('owner',   'settlement.write',         true, '{}'::jsonb),
    ('owner',   'settlement.reconcile',     true, '{}'::jsonb),
    ('owner',   'accounting.rules.read',    true, '{}'::jsonb),
    ('owner',   'accounting.rules.write',   true, '{}'::jsonb),
    ('manager', 'settlement.read',          true, '{}'::jsonb),
    ('manager', 'settlement.write',         true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN permissions p ON p.key = rp.perm_key
JOIN roles r ON lower(r.name) = lower(rp.role_name)
ON CONFLICT (role_id, permission_id) DO NOTHING;


-- ════════════════════════════════════════════════════════════════════════════
-- 5. VALID REFERENCE TYPES — settlement, gateway_fee
-- ════════════════════════════════════════════════════════════════════════════

-- No DDL needed — reference_type is VARCHAR.
-- New valid types: 'settlement', 'gateway_fee'

COMMIT;
