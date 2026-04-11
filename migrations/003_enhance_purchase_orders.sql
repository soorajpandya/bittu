-- ============================================================================
-- MIGRATION 003: Enhance purchase_orders + create kitchen_stations table
-- Run this in Supabase SQL Editor AFTER 002_fix_kyc_verifications.sql
-- ============================================================================

-- 1. Create kitchen_stations table (referenced by kitchen_stations API)
CREATE TABLE IF NOT EXISTS kitchen_stations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kitchen_stations_user_id ON kitchen_stations(user_id);

-- 2. Add new columns to purchase_orders
ALTER TABLE purchase_orders
  ADD COLUMN IF NOT EXISTS po_number VARCHAR(50),
  ADD COLUMN IF NOT EXISTS source_type VARCHAR(20) NOT NULL DEFAULT 'supplier',
  ADD COLUMN IF NOT EXISTS source_id TEXT,
  ADD COLUMN IF NOT EXISTS source_name VARCHAR(255),
  ADD COLUMN IF NOT EXISTS delivery_time TIME,
  ADD COLUMN IF NOT EXISTS sub_total NUMERIC(12, 2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS delivery_charges NUMERIC(12, 2) NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) NOT NULL DEFAULT 'unpaid';

-- Unique constraint on po_number (scoped to user_id)
CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_orders_po_number
  ON purchase_orders(user_id, po_number) WHERE po_number IS NOT NULL;

-- 3. Add amount column to purchase_order_items
ALTER TABLE purchase_order_items
  ADD COLUMN IF NOT EXISTS amount NUMERIC(12, 2) NOT NULL DEFAULT 0;

-- 4. Create a sequence for auto-generating PO numbers per tenant
-- We use a simple global sequence; the service prefixes with tenant info
CREATE SEQUENCE IF NOT EXISTS po_number_seq START 1001;
