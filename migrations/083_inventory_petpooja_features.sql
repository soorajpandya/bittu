-- ════════════════════════════════════════════════════════════════════════════
-- Migration 083 — PETPOOJA-PARITY INVENTORY FEATURES
--
-- Adds the remaining Petpooja-style inventory capabilities on top of the
-- event-sourced ledger (migration 035). ADDITIVE ONLY — no existing column is
-- dropped or reshaped.
--
--   1. Conversion / semi-finished goods  (raw materials → dosa batter, etc.)
--   2. Raw material sales                 (sell raw stock to other parties)
--   3. Outlet-to-outlet returns           (return previously-transferred stock)
--   4. Purchase-order approval workflow   (submit → approve/reject)
--
-- New ledger transaction types: conversion_in, conversion_out, sale.
-- ════════════════════════════════════════════════════════════════════════════

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- 1. Extend INVENTORY_LEDGER transaction_type CHECK for the new event types
-- ────────────────────────────────────────────────────────────────────────────
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
        'purchase',
        'consumption',
        'adjustment_in',
        'adjustment_out',
        'wastage',
        'expired',
        'transfer_in',
        'transfer_out',
        'return_to_vendor',
        'return',
        'restock_cancelled_order',
        'recount',
        'conversion_in',             -- semi-finished good produced
        'conversion_out',            -- raw material consumed by a conversion
        'sale'                       -- raw material sold to another party
    ));

-- ────────────────────────────────────────────────────────────────────────────
-- 2. CONVERSION / SEMI-FINISHED GOODS
-- ────────────────────────────────────────────────────────────────────────────

-- Flag an ingredient as a produced (semi-finished) item, e.g. dosa batter.
ALTER TABLE ingredients
    ADD COLUMN IF NOT EXISTS is_semi_finished BOOLEAN NOT NULL DEFAULT false;

-- A conversion recipe: how much of which raw materials yields the output good.
CREATE TABLE IF NOT EXISTS conversion_recipes (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id        UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id            UUID,
    output_ingredient_id TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    name                 VARCHAR(200),
    yield_quantity       NUMERIC(12,3) NOT NULL CHECK (yield_quantity > 0),
    yield_unit           VARCHAR(40),
    is_active            BOOLEAN NOT NULL DEFAULT true,
    notes                TEXT,
    created_by           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_conversion_recipes_restaurant
    ON conversion_recipes(restaurant_id, is_active)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_conversion_recipes_output
    ON conversion_recipes(output_ingredient_id);

CREATE TABLE IF NOT EXISTS conversion_recipe_inputs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversion_recipe_id UUID NOT NULL REFERENCES conversion_recipes(id) ON DELETE CASCADE,
    ingredient_id        TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    quantity_required    NUMERIC(12,3) NOT NULL CHECK (quantity_required > 0),
    unit                 VARCHAR(40),
    waste_percent        NUMERIC(6,3) NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_conversion_recipe_inputs_recipe
    ON conversion_recipe_inputs(conversion_recipe_id);

-- A conversion run (production event): consumed inputs → produced output.
CREATE TABLE IF NOT EXISTS inventory_conversions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id        UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id            UUID,
    conversion_recipe_id UUID REFERENCES conversion_recipes(id) ON DELETE SET NULL,
    output_ingredient_id TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    produced_quantity    NUMERIC(12,3) NOT NULL CHECK (produced_quantity > 0),
    output_unit          VARCHAR(40),
    correlation_id       UUID NOT NULL DEFAULT gen_random_uuid(),
    status               VARCHAR(20) NOT NULL DEFAULT 'completed'
                            CHECK (status IN ('completed', 'cancelled')),
    notes                TEXT,
    created_by           TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_inventory_conversions_restaurant
    ON inventory_conversions(restaurant_id, created_at DESC);

-- ────────────────────────────────────────────────────────────────────────────
-- 3. RAW MATERIAL SALES
-- ────────────────────────────────────────────────────────────────────────────

CREATE SEQUENCE IF NOT EXISTS inv_sale_number_seq START 1001;

CREATE TABLE IF NOT EXISTS inventory_sales (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id  UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    branch_id      UUID,
    sale_number    VARCHAR(40) NOT NULL DEFAULT ('SAL-' || nextval('inv_sale_number_seq')),
    buyer_name     VARCHAR(200),
    buyer_gst      VARCHAR(20),
    buyer_contact  VARCHAR(120),
    buyer_address  TEXT,
    sub_total      NUMERIC(14,2) NOT NULL DEFAULT 0,
    tax_amount     NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_amount   NUMERIC(14,2) NOT NULL DEFAULT 0,
    status         VARCHAR(20) NOT NULL DEFAULT 'confirmed'
                      CHECK (status IN ('draft', 'confirmed', 'cancelled')),
    terms          TEXT,
    notes          TEXT,
    created_by     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (restaurant_id, sale_number)
);

CREATE INDEX IF NOT EXISTS idx_inventory_sales_restaurant
    ON inventory_sales(restaurant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS inventory_sale_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sale_id       UUID NOT NULL REFERENCES inventory_sales(id) ON DELETE CASCADE,
    ingredient_id TEXT NOT NULL REFERENCES ingredients(id) ON DELETE CASCADE,
    quantity      NUMERIC(12,3) NOT NULL CHECK (quantity > 0),
    unit          VARCHAR(40),
    unit_price    NUMERIC(12,2) NOT NULL DEFAULT 0,
    tax_percent   NUMERIC(6,3) NOT NULL DEFAULT 0,
    line_total    NUMERIC(14,2) NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_inventory_sale_items_sale
    ON inventory_sale_items(sale_id);

-- ────────────────────────────────────────────────────────────────────────────
-- 4. OUTLET-TO-OUTLET RETURNS  (extend stock_transfers)
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE stock_transfers
    ADD COLUMN IF NOT EXISTS transfer_type        VARCHAR(20) NOT NULL DEFAULT 'transfer',
    ADD COLUMN IF NOT EXISTS original_transfer_id UUID REFERENCES stock_transfers(id);

CREATE INDEX IF NOT EXISTS idx_stock_transfers_original
    ON stock_transfers(original_transfer_id)
    WHERE original_transfer_id IS NOT NULL;

-- ────────────────────────────────────────────────────────────────────────────
-- 5. PURCHASE-ORDER APPROVAL WORKFLOW  (extend purchase_orders)
-- ────────────────────────────────────────────────────────────────────────────

ALTER TABLE purchase_orders
    ADD COLUMN IF NOT EXISTS approval_status  VARCHAR(20) NOT NULL DEFAULT 'draft',
    ADD COLUMN IF NOT EXISTS requested_by     TEXT,
    ADD COLUMN IF NOT EXISTS approved_by      TEXT,
    ADD COLUMN IF NOT EXISTS approved_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS rejected_reason  TEXT;

CREATE INDEX IF NOT EXISTS idx_purchase_orders_approval
    ON purchase_orders(approval_status);

-- New RBAC permission for approving/rejecting POs; granted to owner + manager.
INSERT INTO permissions (key) VALUES ('purchase_order.approve')
ON CONFLICT (key) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key = 'purchase_order.approve'
 WHERE r.name IN ('owner', 'manager')
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
