-- ════════════════════════════════════════════════════════════════════════════
-- Migration 035 — INVENTORY EVENT SYSTEM (Section 1: Database Design)
--
-- Purpose:
--   Upgrade the existing simple-mutation inventory tables (`ingredients`,
--   `inventory_transactions`) into a production-grade, event-sourced,
--   restaurant-focused inventory subsystem.
--
-- Strategy (NON-BREAKING / ADDITIVE ONLY):
--   • Existing tables (`ingredients`, `inventory_transactions`,
--     `inventory_ledger`, `recipes`, `recipe_ingredients`, `vendors`,
--     `purchase_orders`, `goods_receipt_notes`, `stock_transfers`) remain
--     unchanged in shape; columns are only ADDED, never dropped.
--   • A new canonical event view (`inventory_events`) is layered over
--     `inventory_ledger` so existing writers keep working while new code
--     reads/writes through the event lens.
--   • New tables: snapshots, adjustments, batches, unit_conversions,
--     alerts, counts, count_items, wastage, expiry, analytics.
--   • All new tables enforce branch-level tenancy (restaurant_id + branch_id),
--     append-only audit columns, soft-delete via `deleted_at` where mutable.
--
-- Concurrency / integrity rules embedded:
--   • inventory_ledger CHECK extended for new event types.
--   • inventory_snapshots (restaurant_id, branch_id, ingredient_id,
--     period_end) is the materialised "as-of" balance; rebuildable from
--     ledger.
--   • Every event row carries an `event_id` UUID for idempotency.
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- 1. Extend INVENTORY_LEDGER with restaurant-grade event types + idempotency
-- ────────────────────────────────────────────────────────────────────────────

-- Drop the old narrow CHECK if it exists, then re-add a wider one.
DO $$
DECLARE
    v_constraint_name TEXT;
BEGIN
    SELECT conname INTO v_constraint_name
    FROM pg_constraint
    WHERE conrelid = 'inventory_ledger'::regclass
      AND contype  = 'c'
      AND pg_get_constraintdef(oid) ILIKE '%transaction_type%IN%';

    IF v_constraint_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE inventory_ledger DROP CONSTRAINT %I', v_constraint_name);
    END IF;
END $$;

ALTER TABLE inventory_ledger
    ADD CONSTRAINT inventory_ledger_transaction_type_check
    CHECK (transaction_type IN (
        'opening',
        'purchase',                  -- GRN verified, supplier delivery
        'consumption',               -- recipe-driven order consumption
        'adjustment_in',             -- manual positive correction
        'adjustment_out',            -- manual negative correction
        'wastage',                   -- spoilage / breakage
        'expired',                   -- batch expiry
        'transfer_in',               -- received from another branch
        'transfer_out',              -- sent to another branch
        'return_to_vendor',          -- supplier return
        'return',                    -- legacy / customer-side return
        'restock_cancelled_order',   -- automatic reversal on order cancel
        'recount'                    -- physical-count reconciliation delta
    ));

-- Idempotency / correlation columns
ALTER TABLE inventory_ledger
    ADD COLUMN IF NOT EXISTS event_id        UUID UNIQUE DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS correlation_id  UUID,
    ADD COLUMN IF NOT EXISTS dedup_key       TEXT,
    ADD COLUMN IF NOT EXISTS batch_id        UUID,
    ADD COLUMN IF NOT EXISTS source          VARCHAR(40)  DEFAULT 'system',
    ADD COLUMN IF NOT EXISTS metadata        JSONB        DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS reversed_by     UUID,                        -- ledger event reversing this one
    ADD COLUMN IF NOT EXISTS reverses_event  UUID,                        -- if this event is itself a reversal
    ADD COLUMN IF NOT EXISTS occurred_at     TIMESTAMPTZ DEFAULT NOW();

-- (dedup_key, restaurant_id) must be unique to make event ingestion idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS uq_inv_ledger_dedup
    ON inventory_ledger (restaurant_id, dedup_key)
    WHERE dedup_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inv_ledger_correlation
    ON inventory_ledger (correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inv_ledger_batch
    ON inventory_ledger (batch_id)
    WHERE batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_inv_ledger_occurred
    ON inventory_ledger (restaurant_id, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_inv_ledger_branch_ing_time
    ON inventory_ledger (branch_id, ingredient_id, occurred_at DESC)
    WHERE branch_id IS NOT NULL;

-- A canonical event-oriented view (richer column names, no schema rewrite).
CREATE OR REPLACE VIEW inventory_events AS
SELECT
    l.event_id,
    l.id                        AS ledger_id,
    l.restaurant_id,
    l.branch_id,
    l.ingredient_id,
    l.transaction_type          AS event_type,
    l.quantity_in,
    l.quantity_out,
    (l.quantity_in - l.quantity_out)   AS delta,
    l.unit_cost,
    (COALESCE(l.unit_cost,0) * (l.quantity_in - l.quantity_out)) AS value_delta,
    l.reference_type,
    l.reference_id,
    l.correlation_id,
    l.dedup_key,
    l.batch_id,
    l.source,
    l.metadata,
    l.reversed_by,
    l.reverses_event,
    l.notes,
    l.created_by,
    l.occurred_at,
    l.created_at
FROM inventory_ledger l;


-- ────────────────────────────────────────────────────────────────────────────
-- 2. Extend INGREDIENTS for restaurant-grade master data
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE ingredients
    ADD COLUMN IF NOT EXISTS branch_id           UUID,
    ADD COLUMN IF NOT EXISTS sku                 VARCHAR(64),
    ADD COLUMN IF NOT EXISTS barcode             VARCHAR(64),
    ADD COLUMN IF NOT EXISTS category            VARCHAR(100),
    ADD COLUMN IF NOT EXISTS storage_location    VARCHAR(100),
    ADD COLUMN IF NOT EXISTS reorder_point       NUMERIC(12,3),
    ADD COLUMN IF NOT EXISTS reorder_quantity    NUMERIC(12,3),
    ADD COLUMN IF NOT EXISTS shelf_life_days     INTEGER,
    ADD COLUMN IF NOT EXISTS is_perishable       BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS track_batches       BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS preferred_vendor_id UUID REFERENCES vendors(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS deleted_at          TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_ingredients_branch
    ON ingredients (branch_id) WHERE branch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ingredients_active
    ON ingredients (restaurant_id, is_active) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_ingredients_sku
    ON ingredients (restaurant_id, sku) WHERE sku IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 3. INVENTORY SNAPSHOTS (materialised period balances for fast reads)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    ingredient_id   TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    period          VARCHAR(20) NOT NULL DEFAULT 'rolling'
                    CHECK (period IN ('rolling','daily','weekly','monthly','manual')),
    opening_qty     NUMERIC(14,3) NOT NULL DEFAULT 0,
    in_qty          NUMERIC(14,3) NOT NULL DEFAULT 0,
    out_qty         NUMERIC(14,3) NOT NULL DEFAULT 0,
    closing_qty     NUMERIC(14,3) NOT NULL DEFAULT 0,
    avg_unit_cost   NUMERIC(12,4) NOT NULL DEFAULT 0,
    valuation       NUMERIC(14,2) NOT NULL DEFAULT 0,
    last_event_id   UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (restaurant_id, branch_id, ingredient_id, period, snapshot_at)
);

CREATE INDEX IF NOT EXISTS idx_inv_snap_lookup
    ON inventory_snapshots (restaurant_id, ingredient_id, snapshot_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_snap_branch
    ON inventory_snapshots (branch_id, ingredient_id, snapshot_at DESC)
    WHERE branch_id IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 4. INVENTORY ADJUSTMENTS (operator-initiated corrections, audited)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    ingredient_id   TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    adjustment_type VARCHAR(20) NOT NULL
                    CHECK (adjustment_type IN ('increase','decrease','recount','damage','theft','found')),
    quantity        NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    unit            VARCHAR(50),
    unit_cost       NUMERIC(12,4),
    reason          VARCHAR(255),
    notes           TEXT,
    ledger_event_id UUID,                     -- ↔ inventory_ledger.event_id
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inv_adj_restaurant
    ON inventory_adjustments (restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_adj_ingredient
    ON inventory_adjustments (ingredient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_adj_branch
    ON inventory_adjustments (branch_id) WHERE branch_id IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 5. INVENTORY BATCHES (FEFO for perishables / GRN-tracked stock)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_batches (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    ingredient_id       TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    batch_number        VARCHAR(100) NOT NULL,
    grn_id              UUID REFERENCES goods_receipt_notes(id) ON DELETE SET NULL,
    vendor_id           UUID REFERENCES vendors(id) ON DELETE SET NULL,
    received_quantity   NUMERIC(14,3) NOT NULL CHECK (received_quantity > 0),
    remaining_quantity  NUMERIC(14,3) NOT NULL CHECK (remaining_quantity >= 0),
    unit                VARCHAR(50),
    unit_cost           NUMERIC(12,4) NOT NULL DEFAULT 0,
    received_date       DATE NOT NULL DEFAULT CURRENT_DATE,
    manufacture_date    DATE,
    expiry_date         DATE,
    status              VARCHAR(20) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','depleted','expired','quarantined','recalled')),
    notes               TEXT,
    created_by          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (restaurant_id, ingredient_id, batch_number)
);

CREATE INDEX IF NOT EXISTS idx_inv_batches_active
    ON inventory_batches (restaurant_id, ingredient_id, expiry_date)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_inv_batches_expiring
    ON inventory_batches (restaurant_id, expiry_date)
    WHERE status = 'active' AND expiry_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inv_batches_branch
    ON inventory_batches (branch_id) WHERE branch_id IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 6. UNIT CONVERSIONS (kg ↔ g, l ↔ ml, packs, dozens, etc.)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS unit_conversions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID REFERENCES restaurants(id) ON DELETE CASCADE,  -- NULL = global default
    ingredient_id   TEXT REFERENCES ingredients(id) ON DELETE CASCADE,  -- NULL = applies to all using same units
    from_unit       VARCHAR(50) NOT NULL,
    to_unit         VARCHAR(50) NOT NULL,
    factor          NUMERIC(18,8) NOT NULL CHECK (factor > 0),
    is_active       BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (restaurant_id, ingredient_id, from_unit, to_unit)
);

CREATE INDEX IF NOT EXISTS idx_unit_conv_lookup
    ON unit_conversions (from_unit, to_unit) WHERE is_active = true;

-- Seed the universally-true conversions (NULL restaurant = global).
INSERT INTO unit_conversions (restaurant_id, ingredient_id, from_unit, to_unit, factor)
VALUES
    (NULL, NULL, 'kg', 'g',  1000),
    (NULL, NULL, 'g',  'kg', 0.001),
    (NULL, NULL, 'l',  'ml', 1000),
    (NULL, NULL, 'ml', 'l',  0.001),
    (NULL, NULL, 'dozen', 'piece', 12),
    (NULL, NULL, 'piece', 'dozen', 0.0833333333)
ON CONFLICT DO NOTHING;


-- ────────────────────────────────────────────────────────────────────────────
-- 7. INVENTORY ALERTS (low-stock, expiry, recount, mismatch)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    ingredient_id   TEXT REFERENCES ingredients(id) ON DELETE CASCADE,
    batch_id        UUID REFERENCES inventory_batches(id) ON DELETE CASCADE,
    alert_type      VARCHAR(30) NOT NULL
                    CHECK (alert_type IN (
                        'low_stock','out_of_stock','expiring_soon','expired',
                        'negative_stock','reorder_point','price_spike','count_mismatch'
                    )),
    severity        VARCHAR(10) NOT NULL DEFAULT 'warning'
                    CHECK (severity IN ('info','warning','critical')),
    title           VARCHAR(255) NOT NULL,
    message         TEXT,
    payload         JSONB DEFAULT '{}'::jsonb,
    status          VARCHAR(20) NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','acknowledged','resolved','suppressed')),
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inv_alerts_open
    ON inventory_alerts (restaurant_id, status, created_at DESC)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_inv_alerts_branch
    ON inventory_alerts (branch_id, status) WHERE branch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inv_alerts_ingredient
    ON inventory_alerts (ingredient_id, created_at DESC) WHERE ingredient_id IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 8. INVENTORY COUNTS (physical stock-takes) + ITEMS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_counts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    count_number    VARCHAR(50) NOT NULL,
    count_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    count_type      VARCHAR(20) NOT NULL DEFAULT 'partial'
                    CHECK (count_type IN ('full','partial','spot','cycle')),
    status          VARCHAR(20) NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','in_progress','completed','approved','cancelled')),
    started_by      TEXT,
    completed_by    TEXT,
    approved_by     TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    approved_at     TIMESTAMPTZ,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (restaurant_id, count_number)
);

CREATE INDEX IF NOT EXISTS idx_inv_counts_status
    ON inventory_counts (restaurant_id, status, count_date DESC);
CREATE INDEX IF NOT EXISTS idx_inv_counts_branch
    ON inventory_counts (branch_id, count_date DESC) WHERE branch_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS inventory_count_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    count_id        UUID NOT NULL REFERENCES inventory_counts(id) ON DELETE CASCADE,
    ingredient_id   TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    expected_qty    NUMERIC(14,3) NOT NULL DEFAULT 0,
    counted_qty     NUMERIC(14,3),
    variance        NUMERIC(14,3) GENERATED ALWAYS AS
                    (COALESCE(counted_qty,0) - expected_qty) STORED,
    unit            VARCHAR(50),
    unit_cost       NUMERIC(12,4),
    notes           TEXT,
    counted_by      TEXT,
    counted_at      TIMESTAMPTZ,
    UNIQUE (count_id, ingredient_id)
);

CREATE INDEX IF NOT EXISTS idx_inv_count_items_count
    ON inventory_count_items (count_id);


-- ────────────────────────────────────────────────────────────────────────────
-- 9. INVENTORY WASTAGE LOG (operator-driven, distinct from ledger event)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_wastage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id       UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    ingredient_id   TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    batch_id        UUID REFERENCES inventory_batches(id) ON DELETE SET NULL,
    quantity        NUMERIC(14,3) NOT NULL CHECK (quantity > 0),
    unit            VARCHAR(50),
    unit_cost       NUMERIC(12,4),
    waste_reason    VARCHAR(40) NOT NULL
                    CHECK (waste_reason IN (
                        'spoilage','expiry','breakage','overcooked','customer_return',
                        'preparation_loss','contamination','other'
                    )),
    notes           TEXT,
    photo_url       TEXT,
    ledger_event_id UUID,
    approved_by     TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inv_wastage_restaurant
    ON inventory_wastage (restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_wastage_ingredient
    ON inventory_wastage (ingredient_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inv_wastage_branch
    ON inventory_wastage (branch_id) WHERE branch_id IS NOT NULL;


-- ────────────────────────────────────────────────────────────────────────────
-- 10. INVENTORY EXPIRY TRACKING (denormalised view for dashboards)
-- ────────────────────────────────────────────────────────────────────────────

CREATE OR REPLACE VIEW inventory_expiry_status AS
SELECT
    b.id                                AS batch_id,
    b.restaurant_id,
    b.branch_id,
    b.ingredient_id,
    i.name                              AS ingredient_name,
    b.batch_number,
    b.remaining_quantity,
    b.unit,
    b.unit_cost,
    b.expiry_date,
    (b.expiry_date - CURRENT_DATE)      AS days_to_expiry,
    CASE
        WHEN b.expiry_date IS NULL                              THEN 'no_expiry'
        WHEN b.expiry_date <  CURRENT_DATE                      THEN 'expired'
        WHEN b.expiry_date <= CURRENT_DATE + INTERVAL '3 days'  THEN 'critical'
        WHEN b.expiry_date <= CURRENT_DATE + INTERVAL '7 days'  THEN 'warning'
        ELSE 'ok'
    END                                 AS expiry_bucket
FROM inventory_batches b
JOIN ingredients i ON i.id = b.ingredient_id
WHERE b.status = 'active'
  AND b.remaining_quantity > 0;


-- ────────────────────────────────────────────────────────────────────────────
-- 11. INVENTORY ANALYTICS (rolling KPIs per ingredient/branch/day)
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS inventory_analytics (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id       UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id           UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
    ingredient_id       TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    period_date         DATE NOT NULL,
    consumed_qty        NUMERIC(14,3) NOT NULL DEFAULT 0,
    purchased_qty       NUMERIC(14,3) NOT NULL DEFAULT 0,
    wasted_qty          NUMERIC(14,3) NOT NULL DEFAULT 0,
    transferred_in      NUMERIC(14,3) NOT NULL DEFAULT 0,
    transferred_out     NUMERIC(14,3) NOT NULL DEFAULT 0,
    adjusted_qty        NUMERIC(14,3) NOT NULL DEFAULT 0,
    closing_qty         NUMERIC(14,3) NOT NULL DEFAULT 0,
    avg_unit_cost       NUMERIC(12,4) NOT NULL DEFAULT 0,
    cogs                NUMERIC(14,2) NOT NULL DEFAULT 0,
    waste_value         NUMERIC(14,2) NOT NULL DEFAULT 0,
    valuation           NUMERIC(14,2) NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (restaurant_id, branch_id, ingredient_id, period_date)
);

CREATE INDEX IF NOT EXISTS idx_inv_analytics_restaurant
    ON inventory_analytics (restaurant_id, period_date DESC);
CREATE INDEX IF NOT EXISTS idx_inv_analytics_ingredient
    ON inventory_analytics (ingredient_id, period_date DESC);


-- ────────────────────────────────────────────────────────────────────────────
-- 12. CALCULATION FUNCTIONS (event-sourced stock balances)
-- ────────────────────────────────────────────────────────────────────────────

-- Live balance from ledger (optionally branch-scoped, optionally as-of).
CREATE OR REPLACE FUNCTION fn_inventory_balance(
    p_ingredient_id TEXT,
    p_branch_id     UUID DEFAULT NULL,
    p_as_of         TIMESTAMPTZ DEFAULT NULL
) RETURNS NUMERIC AS $$
DECLARE
    v_qty NUMERIC;
BEGIN
    SELECT COALESCE(SUM(quantity_in - quantity_out), 0)
      INTO v_qty
      FROM inventory_ledger
     WHERE ingredient_id = p_ingredient_id
       AND (p_branch_id IS NULL OR branch_id = p_branch_id)
       AND (p_as_of     IS NULL OR occurred_at <= p_as_of);
    RETURN v_qty;
END;
$$ LANGUAGE plpgsql STABLE;

-- Append a ledger event idempotently (NULL p_dedup_key disables dedup).
CREATE OR REPLACE FUNCTION fn_inventory_append_event(
    p_restaurant_id    UUID,
    p_branch_id        UUID,
    p_ingredient_id    TEXT,
    p_event_type       VARCHAR,
    p_quantity_in      NUMERIC,
    p_quantity_out     NUMERIC,
    p_unit_cost        NUMERIC,
    p_reference_type   VARCHAR,
    p_reference_id     TEXT,
    p_dedup_key        TEXT,
    p_correlation_id   UUID,
    p_batch_id         UUID,
    p_source           VARCHAR,
    p_metadata         JSONB,
    p_notes            TEXT,
    p_created_by       TEXT
) RETURNS UUID AS $$
DECLARE
    v_event_id UUID;
BEGIN
    -- Idempotency short-circuit
    IF p_dedup_key IS NOT NULL THEN
        SELECT event_id INTO v_event_id
          FROM inventory_ledger
         WHERE restaurant_id = p_restaurant_id
           AND dedup_key = p_dedup_key;
        IF v_event_id IS NOT NULL THEN
            RETURN v_event_id;
        END IF;
    END IF;

    INSERT INTO inventory_ledger (
        restaurant_id, branch_id, ingredient_id, transaction_type,
        quantity_in, quantity_out, unit_cost,
        reference_type, reference_id, notes, created_by,
        dedup_key, correlation_id, batch_id, source, metadata, occurred_at
    ) VALUES (
        p_restaurant_id, p_branch_id, p_ingredient_id, p_event_type,
        COALESCE(p_quantity_in,0), COALESCE(p_quantity_out,0), COALESCE(p_unit_cost,0),
        p_reference_type, p_reference_id, p_notes, p_created_by,
        p_dedup_key, p_correlation_id, p_batch_id,
        COALESCE(p_source,'system'), COALESCE(p_metadata,'{}'::jsonb), NOW()
    )
    RETURNING event_id INTO v_event_id;

    RETURN v_event_id;
END;
$$ LANGUAGE plpgsql;


-- Reverse a previous ledger event (used for cancelled orders / refunds).
CREATE OR REPLACE FUNCTION fn_inventory_reverse_event(
    p_original_event_id UUID,
    p_reversal_type     VARCHAR,
    p_notes             TEXT,
    p_created_by        TEXT
) RETURNS UUID AS $$
DECLARE
    r           inventory_ledger%ROWTYPE;
    v_new_id    UUID;
BEGIN
    SELECT * INTO r FROM inventory_ledger WHERE event_id = p_original_event_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'inventory event % not found', p_original_event_id;
    END IF;

    -- Already reversed?
    IF r.reversed_by IS NOT NULL THEN
        RETURN r.reversed_by;
    END IF;

    INSERT INTO inventory_ledger (
        restaurant_id, branch_id, ingredient_id, transaction_type,
        quantity_in, quantity_out, unit_cost,
        reference_type, reference_id, notes, created_by,
        correlation_id, batch_id, source, metadata, reverses_event, occurred_at
    ) VALUES (
        r.restaurant_id, r.branch_id, r.ingredient_id, p_reversal_type,
        r.quantity_out,         -- swap in/out to reverse
        r.quantity_in,
        r.unit_cost,
        r.reference_type, r.reference_id, p_notes, p_created_by,
        r.correlation_id, r.batch_id, 'reversal', r.metadata,
        r.event_id, NOW()
    )
    RETURNING event_id INTO v_new_id;

    UPDATE inventory_ledger SET reversed_by = v_new_id WHERE event_id = r.event_id;
    RETURN v_new_id;
END;
$$ LANGUAGE plpgsql;


COMMIT;
