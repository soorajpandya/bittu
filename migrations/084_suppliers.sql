-- Migration 084 — Supplier Management
--
-- Adds a dedicated `suppliers` master table for restaurant procurement:
-- basic contact, address, tax/compliance (GSTIN/PAN/FSSAI), purchase terms,
-- payment terms and optional banking details.
--
-- This is ADDITIVE and independent of the legacy ERP `vendors` table
-- (migration 006), which stays wired to GRN / purchase-order / expense
-- sub-ledgers. The new `/api/v1/suppliers` CRUD surface uses THIS table
-- exclusively so the existing procurement pipeline is untouched.
--
-- Tenant model mirrors `purchase_orders`: rows are scoped by `user_id`
-- (the owner) + optional `branch_id`, matching app/core/tenant.py.

BEGIN;

-- Human-friendly, globally-unique supplier codes: SUP-000001, SUP-000002, ...
CREATE SEQUENCE IF NOT EXISTS suppliers_code_seq START 1;

CREATE TABLE IF NOT EXISTS suppliers (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              UUID          NOT NULL,      -- tenant owner (see tenant.py)
    restaurant_id        UUID,
    branch_id            UUID,

    supplier_code        TEXT          NOT NULL UNIQUE
        DEFAULT ('SUP-' || LPAD(nextval('suppliers_code_seq')::text, 6, '0')),

    -- Basic information
    supplier_name        TEXT          NOT NULL,
    business_name        TEXT,
    contact_person       TEXT,
    mobile               TEXT          NOT NULL,
    alternate_phone      TEXT,
    email                TEXT,

    -- Address
    address_line1        TEXT,
    address_line2        TEXT,
    city                 TEXT,
    state                TEXT,
    country              TEXT          NOT NULL DEFAULT 'India',
    pincode              TEXT,

    -- Tax & compliance (India)
    gstin                TEXT,
    pan                  TEXT,
    fssai_license        TEXT,

    -- Purchase information
    category             TEXT,
    min_order_value      NUMERIC(14,2) NOT NULL DEFAULT 0,
    lead_time_days       INTEGER,
    is_preferred         BOOLEAN       NOT NULL DEFAULT FALSE,

    -- Payment details
    payment_terms        TEXT          NOT NULL DEFAULT 'cash',
    credit_period_days   INTEGER       NOT NULL DEFAULT 0,
    credit_limit         NUMERIC(14,2) NOT NULL DEFAULT 0,
    opening_balance      NUMERIC(14,2) NOT NULL DEFAULT 0,
    current_outstanding  NUMERIC(14,2) NOT NULL DEFAULT 0,   -- calculated (starts at opening_balance)

    -- Banking (optional)
    bank_name            TEXT,
    account_number       TEXT,
    ifsc                 TEXT,
    upi_id               TEXT,

    -- System
    status               TEXT          NOT NULL DEFAULT 'active',
    notes                TEXT,
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    deleted_at           TIMESTAMPTZ,

    CONSTRAINT ck_suppliers_status
        CHECK (status IN ('active', 'inactive')),
    CONSTRAINT ck_suppliers_payment_terms
        CHECK (payment_terms IN ('cash', 'credit', 'advance')),
    CONSTRAINT ck_suppliers_category
        CHECK (category IS NULL OR category IN (
            'vegetables', 'dairy', 'meat_poultry', 'seafood', 'grocery',
            'beverages', 'packaging', 'cleaning_supplies', 'other'
        ))
);

CREATE INDEX IF NOT EXISTS ix_suppliers_user         ON suppliers (user_id)                       WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_suppliers_user_status  ON suppliers (user_id, status)               WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_suppliers_user_branch  ON suppliers (user_id, branch_id)            WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_suppliers_name_lower   ON suppliers (user_id, lower(supplier_name)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_suppliers_mobile       ON suppliers (user_id, mobile)               WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_suppliers_gstin        ON suppliers (user_id, gstin)                WHERE deleted_at IS NULL;

-- ── Permissions ───────────────────────────────────────────────────────────
-- `permissions` has only (id, key, created_at) — never include a description.
INSERT INTO permissions (key) VALUES
    ('supplier.read'), ('supplier.write'), ('supplier.delete')
ON CONFLICT (key) DO NOTHING;

-- Owner: full supplier control.
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
FROM roles r
CROSS JOIN permissions p
WHERE lower(r.name) = 'owner'
  AND p.key IN ('supplier.read', 'supplier.write', 'supplier.delete')
ON CONFLICT (role_id, permission_id)
DO UPDATE SET allowed = true, updated_at = NOW();

-- Manager: read + write + delete.
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
FROM roles r
CROSS JOIN permissions p
WHERE lower(r.name) = 'manager'
  AND p.key IN ('supplier.read', 'supplier.write', 'supplier.delete')
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
