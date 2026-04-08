-- Migration 007: Bridge restaurant ↔ accounting
-- Adds linking columns so accounting records can trace back to restaurant orders,
-- payments, and customers.  Also deduplicates customer data by linking acc_contacts
-- to the restaurant customers table.

BEGIN;

-- ══════════════════════════════════════════════════════════════
-- acc_invoices: link to restaurant order + payment
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_invoices
    ADD COLUMN IF NOT EXISTS source_order_id   TEXT,
    ADD COLUMN IF NOT EXISTS source_payment_id TEXT;

CREATE INDEX IF NOT EXISTS idx_acc_inv_source_order
    ON acc_invoices(source_order_id) WHERE source_order_id IS NOT NULL;

-- ══════════════════════════════════════════════════════════════
-- acc_credit_notes: link to restaurant order (for refund tracing)
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_credit_notes
    ADD COLUMN IF NOT EXISTS source_order_id TEXT;

CREATE INDEX IF NOT EXISTS idx_acc_cn_source_order
    ON acc_credit_notes(source_order_id) WHERE source_order_id IS NOT NULL;

-- ══════════════════════════════════════════════════════════════
-- acc_contacts: link to restaurant customer
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_contacts
    ADD COLUMN IF NOT EXISTS source_customer_id INT;

CREATE INDEX IF NOT EXISTS idx_acc_contacts_source_cust
    ON acc_contacts(source_customer_id) WHERE source_customer_id IS NOT NULL;

-- ══════════════════════════════════════════════════════════════
-- acc_expenses: link to restaurant purchase orders / inventory
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_expenses
    ADD COLUMN IF NOT EXISTS source_purchase_order_id TEXT;

-- ══════════════════════════════════════════════════════════════
-- acc_items: link to restaurant menu items
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_items
    ADD COLUMN IF NOT EXISTS source_item_id INT;

CREATE INDEX IF NOT EXISTS idx_acc_items_source_item
    ON acc_items(source_item_id) WHERE source_item_id IS NOT NULL;

-- ══════════════════════════════════════════════════════════════
-- acc_bills: link to restaurant purchase orders
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_bills
    ADD COLUMN IF NOT EXISTS source_purchase_order_id TEXT;

-- ══════════════════════════════════════════════════════════════
-- acc_journals: add sub_total column (used by day-book generation)
-- ══════════════════════════════════════════════════════════════
ALTER TABLE acc_journals
    ADD COLUMN IF NOT EXISTS sub_total NUMERIC(20,2) DEFAULT 0;

COMMIT;
