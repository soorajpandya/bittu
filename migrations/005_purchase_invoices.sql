-- Migration 005: Purchase Invoice tables for AI-powered invoice import
-- Run in Supabase SQL Editor

-- ── Purchase Invoices (header) ──
CREATE TABLE IF NOT EXISTS purchase_invoices (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    restaurant_id   TEXT,
    branch_id       TEXT,
    vendor_name     TEXT,
    vendor_gstin    TEXT,
    invoice_number  TEXT,
    invoice_date    DATE,
    subtotal        NUMERIC(12,2) DEFAULT 0,
    tax_amount      NUMERIC(12,2) DEFAULT 0,
    total_amount    NUMERIC(12,2) DEFAULT 0,
    payment_status  TEXT DEFAULT 'unpaid' CHECK (payment_status IN ('unpaid','partial','paid')),
    status          TEXT DEFAULT 'draft' CHECK (status IN ('draft','confirmed','cancelled')),
    purchase_order_id TEXT,                      -- optional link to existing PO
    raw_ocr_text    TEXT,                        -- raw OCR output for debugging
    raw_ai_response JSONB,                       -- full OpenAI response for debugging
    idempotency_key TEXT UNIQUE,                 -- prevent duplicate imports
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_purchase_invoices_user ON purchase_invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_vendor ON purchase_invoices(user_id, vendor_name);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_invoice_no ON purchase_invoices(user_id, invoice_number);
CREATE INDEX IF NOT EXISTS idx_purchase_invoices_idempotency ON purchase_invoices(idempotency_key);

-- ── Purchase Invoice Items (line items) ──
CREATE TABLE IF NOT EXISTS purchase_invoice_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id          UUID NOT NULL REFERENCES purchase_invoices(id) ON DELETE CASCADE,
    ingredient_id       TEXT,                    -- matched/created ingredient
    item_name           TEXT NOT NULL,
    hsn_code            TEXT,
    quantity            NUMERIC(12,3) NOT NULL DEFAULT 0,
    unit                TEXT DEFAULT 'pcs',
    unit_price          NUMERIC(12,2) DEFAULT 0,
    discount_percent    NUMERIC(5,2) DEFAULT 0,
    tax_percent         NUMERIC(5,2) DEFAULT 0,
    tax_amount          NUMERIC(12,2) DEFAULT 0,
    line_total          NUMERIC(12,2) DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_purchase_invoice_items_invoice ON purchase_invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_purchase_invoice_items_ingredient ON purchase_invoice_items(ingredient_id);
