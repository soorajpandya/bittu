-- ============================================================================
-- BITTU BACKEND - COMPLETE DATABASE SCHEMA
-- Run this in Supabase SQL Editor to create all required tables.
-- ============================================================================

-- ────────────────────────────────────────────────────────────────────────────
-- 1. CORE MULTI-TENANT STRUCTURE
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS restaurants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  phone VARCHAR(20),
  email VARCHAR(255),
  address TEXT,
  city VARCHAR(100),
  state VARCHAR(100),
  pincode VARCHAR(10),
  latitude NUMERIC(10, 8),
  longitude NUMERIC(11, 8),
  logo_url TEXT,
  cover_url TEXT,
  gst_number VARCHAR(50),
  fssai_number VARCHAR(50),
  is_active BOOLEAN NOT NULL DEFAULT true,
  opening_time TIME,
  closing_time TIME,
  avg_prep_time INTEGER,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sub_branches (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  is_main_branch BOOLEAN NOT NULL DEFAULT false,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS branch_users (
  user_id TEXT NOT NULL,
  branch_id UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
  owner_id TEXT NOT NULL,
  role VARCHAR(50) NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, branch_id)
);

CREATE TABLE IF NOT EXISTS staff (
  id SERIAL PRIMARY KEY,
  restaurant_id UUID REFERENCES restaurants(id) ON DELETE CASCADE,
  branch_id UUID REFERENCES sub_branches(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL,
  phone VARCHAR(20),
  role VARCHAR(50) NOT NULL DEFAULT 'staff',
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 2. MENU ITEMS & CUSTOMIZATION
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS items (
  "Item_ID" SERIAL PRIMARY KEY,
  "Item_Name" VARCHAR(255) NOT NULL,
  "Description" TEXT,
  price NUMERIC(10, 2) NOT NULL,
  "Available_Status" BOOLEAN NOT NULL DEFAULT true,
  "Category" VARCHAR(100),
  "Subcategory" VARCHAR(100),
  "Cuisine" VARCHAR(100),
  "Spice_Level" VARCHAR(20),
  "Prep_Time_Min" INTEGER,
  "Image_url" TEXT,
  is_veg BOOLEAN,
  tags TEXT[],
  sort_order INTEGER DEFAULT 0,
  dine_in_available BOOLEAN NOT NULL DEFAULT true,
  takeaway_available BOOLEAN NOT NULL DEFAULT true,
  delivery_available BOOLEAN NOT NULL DEFAULT true,
  restaurant_id UUID,
  branch_id UUID,
  user_id TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS item_variants (
  id SERIAL PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  price NUMERIC(10, 2) NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  sku VARCHAR(100),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS item_addons (
  id SERIAL PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  price NUMERIC(10, 2) NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS item_extras (
  id SERIAL PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  price NUMERIC(10, 2) NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS categories (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  name VARCHAR(255) NOT NULL,
  slug VARCHAR(255),
  description TEXT,
  image_url TEXT,
  sort_order INTEGER DEFAULT 0,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS combos (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  restaurant_id UUID,
  name VARCHAR(255) NOT NULL,
  description TEXT,
  price NUMERIC(10, 2) NOT NULL,
  image_url TEXT,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS combo_items (
  id SERIAL PRIMARY KEY,
  combo_id INTEGER NOT NULL REFERENCES combos(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  quantity INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS modifier_groups (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  name VARCHAR(255) NOT NULL,
  is_required BOOLEAN NOT NULL DEFAULT false,
  min_selections INTEGER DEFAULT 0,
  max_selections INTEGER,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS modifier_options (
  id SERIAL PRIMARY KEY,
  group_id INTEGER NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL,
  price NUMERIC(10, 2) NOT NULL DEFAULT 0,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS item_station_mapping (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  station_id INTEGER NOT NULL,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  UNIQUE (item_id, station_id)
);

CREATE TABLE IF NOT EXISTS item_ingredients (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  ingredient_id TEXT,
  quantity_used NUMERIC(10, 3) NOT NULL DEFAULT 0,
  unit VARCHAR(50),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 3. CUSTOMERS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS customers (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  restaurant_id UUID,
  name VARCHAR(255) NOT NULL,
  email VARCHAR(255),
  phone_number VARCHAR(20),
  address TEXT,
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS customer_addresses (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  label VARCHAR(100) NOT NULL DEFAULT 'Home',
  address_line TEXT NOT NULL,
  city VARCHAR(100),
  state VARCHAR(100),
  pincode VARCHAR(10),
  lat NUMERIC(10, 8),
  lng NUMERIC(11, 8),
  is_default BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS favourite_items (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
  restaurant_id UUID,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, item_id)
);

-- ────────────────────────────────────────────────────────────────────────────
-- 4. ORDERS & ORDER MANAGEMENT
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  branch_id UUID,
  restaurant_id UUID,
  customer_id INTEGER,
  source VARCHAR(20) NOT NULL DEFAULT 'pos',
  status VARCHAR(30) NOT NULL DEFAULT 'pending',
  subtotal NUMERIC(12, 2) NOT NULL DEFAULT 0,
  tax_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  table_number VARCHAR(50),
  delivery_address TEXT,
  delivery_phone VARCHAR(20),
  coupon_id INTEGER,
  notes TEXT,
  items JSONB,
  metadata JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS order_items (
  id SERIAL PRIMARY KEY,
  order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  item_id INTEGER REFERENCES items("Item_ID") ON DELETE SET NULL,
  variant_id INTEGER,
  item_name VARCHAR(255),
  quantity INTEGER NOT NULL DEFAULT 1,
  unit_price NUMERIC(10, 2) NOT NULL,
  total_price NUMERIC(12, 2) NOT NULL,
  addons JSONB,
  extras JSONB,
  notes TEXT,
  user_id TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 5. PAYMENTS & BILLING
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  restaurant_id UUID,
  user_id TEXT NOT NULL,
  branch_id UUID,
  method VARCHAR(30) NOT NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'pending',
  amount NUMERIC(12, 2) NOT NULL,
  currency VARCHAR(3) NOT NULL DEFAULT 'INR',
  razorpay_order_id VARCHAR(100),
  razorpay_payment_id VARCHAR(100),
  razorpay_signature TEXT,
  paid_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cash_transactions (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  type VARCHAR(50) NOT NULL,
  amount NUMERIC(12, 2) NOT NULL,
  description TEXT,
  category VARCHAR(100),
  payment_method VARCHAR(50) NOT NULL DEFAULT 'cash',
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS due_payments (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
  total_amount NUMERIC(12, 2) NOT NULL,
  paid_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  due_amount NUMERIC(12, 2),
  status VARCHAR(50) NOT NULL DEFAULT 'pending',
  due_date DATE,
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payment_reminders (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  due_payment_id INTEGER,
  reminder_text TEXT,
  reminder_date DATE,
  is_sent BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 6. SUBSCRIPTIONS & PLANS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS subscription_plans (
  id SERIAL PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  slug VARCHAR(100) NOT NULL UNIQUE,
  description TEXT,
  price NUMERIC(12, 2) NOT NULL DEFAULT 0,
  monthly_price NUMERIC(12, 2),
  currency VARCHAR(3) NOT NULL DEFAULT 'INR',
  interval VARCHAR(20) NOT NULL DEFAULT 'monthly',
  features JSONB,
  limits JSONB,
  not_included JSONB,
  highlight BOOLEAN NOT NULL DEFAULT false,
  highlight_label VARCHAR(100),
  cta_text VARCHAR(100),
  discount_label VARCHAR(100),
  razorpay_plan_id VARCHAR(100),
  is_active BOOLEAN NOT NULL DEFAULT true,
  sort_order INTEGER DEFAULT 0,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_subscriptions (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  plan_id INTEGER REFERENCES subscription_plans(id) ON DELETE SET NULL,
  status VARCHAR(50) NOT NULL DEFAULT 'trialing',
  trial_started_at TIMESTAMP WITH TIME ZONE,
  trial_expires_at TIMESTAMP WITH TIME ZONE,
  trial_end TIMESTAMP WITH TIME ZONE,
  trial_used BOOLEAN NOT NULL DEFAULT false,
  razorpay_subscription_id VARCHAR(100),
  current_period_start TIMESTAMP WITH TIME ZONE,
  current_period_end TIMESTAMP WITH TIME ZONE,
  grace_period_end TIMESTAMP WITH TIME ZONE,
  last_payment_at TIMESTAMP WITH TIME ZONE,
  payment_retry_count INTEGER NOT NULL DEFAULT 0,
  cancelled_at TIMESTAMP WITH TIME ZONE,
  ended_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trial_eligibility (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL UNIQUE,
  trial_started_at TIMESTAMP WITH TIME ZONE,
  trial_expires_at TIMESTAMP WITH TIME ZONE,
  eligible BOOLEAN NOT NULL DEFAULT true,
  used BOOLEAN NOT NULL DEFAULT false,
  used_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS billing_history (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  subscription_id INTEGER,
  invoice_number VARCHAR(50),
  razorpay_payment_id VARCHAR(100),
  amount NUMERIC(12, 2),
  status VARCHAR(50),
  paid_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS invoices (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
  invoice_number VARCHAR(50),
  amount NUMERIC(12, 2),
  tax NUMERIC(12, 2),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 7. COUPONS & OFFERS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS coupons (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  restaurant_id UUID,
  code VARCHAR(50) NOT NULL,
  title VARCHAR(255),
  type VARCHAR(50) NOT NULL,
  discount_value NUMERIC(10, 2) NOT NULL,
  min_order_value NUMERIC(12, 2),
  max_discount NUMERIC(12, 2),
  usage_limit INTEGER,
  user_usage_limit INTEGER,
  valid_from TIMESTAMP WITH TIME ZONE,
  valid_until TIMESTAMP WITH TIME ZONE,
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS coupon_usage (
  id SERIAL PRIMARY KEY,
  coupon_id INTEGER NOT NULL REFERENCES coupons(id) ON DELETE CASCADE,
  customer_id INTEGER,
  order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
  user_id TEXT,
  used_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS offers (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  restaurant_id UUID,
  title VARCHAR(255) NOT NULL,
  description TEXT,
  discount NUMERIC(10, 2),
  code VARCHAR(50),
  type VARCHAR(50),
  icon TEXT,
  expiry_days INTEGER,
  is_active BOOLEAN NOT NULL DEFAULT true,
  valid_from TIMESTAMP WITH TIME ZONE,
  valid_until TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 8. KITCHEN
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS kitchen_orders (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  branch_id UUID,
  restaurant_id UUID,
  status VARCHAR(30) NOT NULL DEFAULT 'queued',
  station VARCHAR(100),
  priority INTEGER DEFAULT 0,
  started_at TIMESTAMP WITH TIME ZONE,
  ready_at TIMESTAMP WITH TIME ZONE,
  served_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS kitchen_order_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kitchen_order_id UUID NOT NULL REFERENCES kitchen_orders(id) ON DELETE CASCADE,
  order_item_id INTEGER REFERENCES order_items(id) ON DELETE SET NULL,
  status VARCHAR(30) NOT NULL DEFAULT 'queued',
  item_id INTEGER REFERENCES items("Item_ID") ON DELETE SET NULL,
  item_name VARCHAR(255),
  quantity INTEGER NOT NULL DEFAULT 1,
  station_id INTEGER,
  started_at TIMESTAMP WITH TIME ZONE,
  ready_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 9. DINE-IN & TABLE MANAGEMENT
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS restaurant_tables (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  restaurant_id UUID,
  table_number VARCHAR(50) NOT NULL,
  capacity INTEGER,
  status VARCHAR(20) NOT NULL DEFAULT 'blank',
  is_active BOOLEAN NOT NULL DEFAULT true,
  is_occupied BOOLEAN NOT NULL DEFAULT false,
  occupied_since TIMESTAMP WITH TIME ZONE,
  session_token VARCHAR(255),
  current_order_id UUID,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dine_in_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  table_id UUID NOT NULL REFERENCES restaurant_tables(id) ON DELETE CASCADE,
  restaurant_id UUID,
  user_id TEXT NOT NULL,
  branch_id UUID,
  session_token VARCHAR(255) NOT NULL,
  device_id VARCHAR(100),
  guest_count INTEGER DEFAULT 1,
  status VARCHAR(50) NOT NULL DEFAULT 'active',
  active_order_id UUID,
  last_activity_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMP WITH TIME ZONE,
  ended_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS table_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  table_id UUID NOT NULL REFERENCES restaurant_tables(id) ON DELETE CASCADE,
  restaurant_id UUID,
  user_id TEXT NOT NULL,
  branch_id UUID,
  session_token VARCHAR(255) NOT NULL,
  guest_count INTEGER NOT NULL DEFAULT 1,
  customer_count INTEGER NOT NULL DEFAULT 1,
  started_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  is_active BOOLEAN NOT NULL DEFAULT true,
  status VARCHAR(50) NOT NULL DEFAULT 'active',
  expires_at TIMESTAMP WITH TIME ZONE,
  ended_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS table_session_carts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id UUID NOT NULL REFERENCES table_sessions(id) ON DELETE CASCADE,
  item_id INTEGER REFERENCES items("Item_ID") ON DELETE SET NULL,
  variant_id INTEGER REFERENCES item_variants(id) ON DELETE SET NULL,
  item_name VARCHAR(255),
  variant_name VARCHAR(255),
  quantity INTEGER NOT NULL DEFAULT 1,
  unit_price NUMERIC(10, 2) NOT NULL,
  total_price NUMERIC(12, 2) NOT NULL,
  addons JSONB,
  extras JSONB,
  notes TEXT,
  added_by VARCHAR(100),
  request_id VARCHAR(100),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS table_session_devices (
  id SERIAL PRIMARY KEY,
  session_id UUID NOT NULL REFERENCES table_sessions(id) ON DELETE CASCADE,
  user_id TEXT,
  branch_id UUID,
  device_id VARCHAR(100) NOT NULL,
  device_name VARCHAR(255),
  last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  is_active BOOLEAN NOT NULL DEFAULT true,
  joined_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  UNIQUE (session_id, device_id)
);

CREATE TABLE IF NOT EXISTS session_orders (
  id SERIAL PRIMARY KEY,
  session_id UUID NOT NULL REFERENCES table_sessions(id) ON DELETE CASCADE,
  order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  role VARCHAR(50),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 10. DELIVERY & LOGISTICS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS delivery_partners (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL,
  phone VARCHAR(20),
  status VARCHAR(20) NOT NULL DEFAULT 'available',
  is_active BOOLEAN NOT NULL DEFAULT true,
  latitude NUMERIC(10, 8),
  longitude NUMERIC(11, 8),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deliveries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  order_id UUID NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  restaurant_id UUID NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
  status VARCHAR(30) NOT NULL DEFAULT 'unassigned',
  pickup_address TEXT,
  delivery_address TEXT NOT NULL,
  delivery_phone VARCHAR(20),
  partner_id UUID REFERENCES delivery_partners(id) ON DELETE SET NULL,
  assigned_at TIMESTAMP WITH TIME ZONE,
  picked_up_at TIMESTAMP WITH TIME ZONE,
  delivered_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS delivery_tracking (
  id SERIAL PRIMARY KEY,
  delivery_id UUID NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
  partner_id UUID NOT NULL REFERENCES delivery_partners(id) ON DELETE CASCADE,
  latitude NUMERIC(10, 8),
  longitude NUMERIC(11, 8),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 11. INVENTORY & PURCHASE ORDERS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ingredients (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  restaurant_id UUID,
  name VARCHAR(255) NOT NULL,
  unit VARCHAR(50),
  current_stock NUMERIC(12, 3) NOT NULL DEFAULT 0,
  stock_quantity NUMERIC(12, 3) NOT NULL DEFAULT 0,
  minimum_stock NUMERIC(12, 3),
  cost_per_unit NUMERIC(10, 2),
  supplier VARCHAR(255),
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inventory_transactions (
  id SERIAL PRIMARY KEY,
  restaurant_id UUID,
  ingredient_id TEXT REFERENCES ingredients(id) ON DELETE SET NULL,
  type VARCHAR(30) NOT NULL,
  quantity NUMERIC(12, 3) NOT NULL,
  unit VARCHAR(50),
  reference_id UUID,
  order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
  performed_by TEXT,
  notes TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS purchase_orders (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  supplier_name VARCHAR(255),
  supplier_contact VARCHAR(20),
  status VARCHAR(50) NOT NULL DEFAULT 'draft',
  notes TEXT,
  expected_delivery_date DATE,
  total_amount NUMERIC(12, 2),
  received_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS purchase_order_items (
  id SERIAL PRIMARY KEY,
  purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
  ingredient_id TEXT REFERENCES ingredients(id) ON DELETE SET NULL,
  ingredient_name VARCHAR(255),
  quantity_ordered NUMERIC(12, 3),
  quantity NUMERIC(12, 3),
  unit VARCHAR(50),
  unit_cost NUMERIC(10, 2),
  unit_price NUMERIC(10, 2),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 12. SETTINGS & CONFIGURATION
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS restaurant_settings (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL UNIQUE,
  restaurant_id UUID,
  tax_percentage NUMERIC(5, 2) NOT NULL DEFAULT 0,
  currency VARCHAR(3) NOT NULL DEFAULT 'INR',
  receipt_header TEXT,
  receipt_footer TEXT,
  auto_accept_orders BOOLEAN NOT NULL DEFAULT false,
  enable_qr_ordering BOOLEAN NOT NULL DEFAULT false,
  enable_delivery BOOLEAN NOT NULL DEFAULT false,
  enable_dine_in BOOLEAN NOT NULL DEFAULT true,
  enable_takeaway BOOLEAN NOT NULL DEFAULT true,
  printer_config JSONB,
  theme_config JSONB,
  enable_led_display BOOLEAN NOT NULL DEFAULT false,
  led_display_url TEXT,
  enable_dual_screen BOOLEAN NOT NULL DEFAULT false,
  dual_screen_url TEXT,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS deliverable_pincodes (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  pincode VARCHAR(10) NOT NULL,
  area_name VARCHAR(255),
  city VARCHAR(100),
  state VARCHAR(100),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 13. ANALYTICS & REPORTING
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS daily_analytics (
  id SERIAL PRIMARY KEY,
  branch_id UUID NOT NULL,
  date DATE NOT NULL,
  total_orders INTEGER NOT NULL DEFAULT 0,
  completed_orders INTEGER NOT NULL DEFAULT 0,
  cancelled_orders INTEGER NOT NULL DEFAULT 0,
  total_revenue NUMERIC(12, 2) NOT NULL DEFAULT 0,
  total_tax NUMERIC(12, 2) NOT NULL DEFAULT 0,
  total_discount NUMERIC(12, 2) NOT NULL DEFAULT 0,
  avg_order_value NUMERIC(12, 2) NOT NULL DEFAULT 0,
  dine_in_orders INTEGER NOT NULL DEFAULT 0,
  takeaway_orders INTEGER NOT NULL DEFAULT 0,
  delivery_orders INTEGER NOT NULL DEFAULT 0,
  cash_orders INTEGER NOT NULL DEFAULT 0,
  online_orders INTEGER NOT NULL DEFAULT 0,
  top_items JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  UNIQUE (branch_id, date)
);

CREATE TABLE IF NOT EXISTS user_funnel_events (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL UNIQUE,
  step VARCHAR(100),
  first_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  last_seen TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  visit_count INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 14. NOTIFICATIONS & ALERTS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS alerts (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  type VARCHAR(50) NOT NULL,
  severity VARCHAR(50) NOT NULL,
  title VARCHAR(255) NOT NULL,
  message TEXT,
  reference_type VARCHAR(50),
  reference_id TEXT,
  is_read BOOLEAN NOT NULL DEFAULT false,
  is_dismissed BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 15. AUDIT, SYNC & IDEMPOTENCY
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS audit_log (
  id SERIAL PRIMARY KEY,
  restaurant_id UUID,
  user_id TEXT NOT NULL,
  action VARCHAR(100),
  entity_type VARCHAR(100),
  entity_id TEXT,
  new_data JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sync_logs (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  sync_action VARCHAR(100),
  synced_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT PRIMARY KEY,
  session_id TEXT,
  result JSONB,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 16. SUPPORT & HELP
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS help_articles (
  id SERIAL PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  category VARCHAR(100),
  content TEXT,
  "order" INTEGER,
  is_published BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feedback (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  branch_id UUID,
  customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL,
  order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
  rating NUMERIC(3, 1),
  food_rating NUMERIC(3, 1),
  service_rating NUMERIC(3, 1),
  ambience_rating NUMERIC(3, 1),
  comment TEXT,
  source VARCHAR(50) NOT NULL DEFAULT 'pos',
  staff_response TEXT,
  responded BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 17. KYC & INTEGRATIONS
-- ────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS kyc_verifications (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  verification_id VARCHAR(100) UNIQUE,
  status VARCHAR(50),
  aadhaar_number VARCHAR(50),
  pan_number VARCHAR(50),
  dl_number VARCHAR(50),
  kyc_data JSONB,
  verified_at TIMESTAMP WITH TIME ZONE,
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS google_connections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  restaurant_id UUID,
  account_id VARCHAR(255),
  location_id VARCHAR(100),
  is_active BOOLEAN NOT NULL DEFAULT true,
  synced_at TIMESTAMP WITH TIME ZONE,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
  created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- ────────────────────────────────────────────────────────────────────────────
-- 18. INDEXES FOR PERFORMANCE
-- ────────────────────────────────────────────────────────────────────────────

-- Multi-tenant isolation
CREATE INDEX IF NOT EXISTS idx_restaurants_owner_id ON restaurants(owner_id);
CREATE INDEX IF NOT EXISTS idx_sub_branches_restaurant_id ON sub_branches(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_sub_branches_owner_id ON sub_branches(owner_id);
CREATE INDEX IF NOT EXISTS idx_branch_users_user_id ON branch_users(user_id);

-- Orders
CREATE INDEX IF NOT EXISTS idx_orders_user_branch ON orders(user_id, branch_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_restaurant_id ON orders(restaurant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id);

-- Items
CREATE INDEX IF NOT EXISTS idx_items_user_id ON items(user_id);
CREATE INDEX IF NOT EXISTS idx_items_branch_id ON items(branch_id);

-- Kitchen operations
CREATE INDEX IF NOT EXISTS idx_kitchen_orders_status ON kitchen_orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kitchen_orders_order_id ON kitchen_orders(order_id);

-- Delivery
CREATE INDEX IF NOT EXISTS idx_deliveries_status ON deliveries(status);
CREATE INDEX IF NOT EXISTS idx_deliveries_partner_id ON deliveries(partner_id);

-- Inventory
CREATE INDEX IF NOT EXISTS idx_ingredients_user_id ON ingredients(user_id);
CREATE INDEX IF NOT EXISTS idx_inventory_txns_reference ON inventory_transactions(reference_id);

-- Customers
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone_number);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_user_id ON customers(user_id);

-- Analytics
CREATE INDEX IF NOT EXISTS idx_daily_analytics_date ON daily_analytics(branch_id, date DESC);

-- Sessions
CREATE INDEX IF NOT EXISTS idx_dine_in_sessions_token ON dine_in_sessions(session_token);
CREATE INDEX IF NOT EXISTS idx_table_sessions_token ON table_sessions(session_token);

-- Subscriptions
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_user_id ON user_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_user_subscriptions_rz_id ON user_subscriptions(razorpay_subscription_id);

-- Audit
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_user_id ON audit_log(user_id);

-- Staff
CREATE INDEX IF NOT EXISTS idx_staff_branch_id ON staff(branch_id);
CREATE INDEX IF NOT EXISTS idx_staff_restaurant_id ON staff(restaurant_id);

-- ════════════════════════════════════════════════════════════════════════════
-- SEED: Default subscription plans (optional, adjust as needed)
-- ════════════════════════════════════════════════════════════════════════════

INSERT INTO subscription_plans (name, slug, description, price, currency, interval, features, is_active, sort_order)
VALUES
  ('Free Trial', 'free-trial', '14-day free trial with full access', 0, 'INR', 'once', '["POS", "QR Ordering", "Kitchen Display", "Basic Analytics"]'::jsonb, true, 0),
  ('Starter', 'starter', 'Perfect for small restaurants', 999, 'INR', 'monthly', '["POS", "QR Ordering", "Kitchen Display", "Analytics", "1 Branch"]'::jsonb, true, 1),
  ('Professional', 'professional', 'For growing restaurants', 2499, 'INR', 'monthly', '["Everything in Starter", "Multi-branch", "Delivery", "Inventory", "Advanced Analytics"]'::jsonb, true, 2),
  ('Enterprise', 'enterprise', 'For restaurant chains', 4999, 'INR', 'monthly', '["Everything in Professional", "Unlimited Branches", "API Access", "Priority Support"]'::jsonb, true, 3)
ON CONFLICT (slug) DO NOTHING;

-- ════════════════════════════════════════════════════════════════════════════
-- END OF SCHEMA
-- ════════════════════════════════════════════════════════════════════════════
