-- Minimal isolated PostgreSQL schema for inventory integration tests.
CREATE TABLE restaurants (id uuid PRIMARY KEY);
CREATE TABLE sub_branches (id uuid PRIMARY KEY, restaurant_id uuid REFERENCES restaurants);
CREATE TABLE ingredients (
 id text PRIMARY KEY, restaurant_id uuid REFERENCES restaurants, branch_id uuid, name text,
 unit text, current_stock numeric DEFAULT 0, stock_quantity numeric DEFAULT 0,
 cost_per_unit numeric DEFAULT 0, minimum_stock numeric DEFAULT 0, reorder_point numeric DEFAULT 0,
 track_batches boolean DEFAULT false, updated_at timestamptz, deleted_at timestamptz, is_active boolean DEFAULT true
);
CREATE TABLE inventory_ledger (
 event_id uuid DEFAULT gen_random_uuid() PRIMARY KEY, restaurant_id uuid REFERENCES restaurants,
 branch_id uuid, ingredient_id text REFERENCES ingredients, transaction_type varchar,
 quantity_in numeric DEFAULT 0, quantity_out numeric DEFAULT 0, unit_cost numeric DEFAULT 0,
 reference_type varchar, reference_id text, notes text, created_by text, dedup_key text,
 correlation_id uuid, batch_id uuid, source text, metadata jsonb, occurred_at timestamptz DEFAULT now(),
 created_at timestamptz DEFAULT now(), reversed_by uuid, reverses_event uuid
);
CREATE UNIQUE INDEX inv_dedup ON inventory_ledger(restaurant_id,dedup_key) WHERE dedup_key IS NOT NULL;
CREATE TABLE inventory_batches (
 id uuid DEFAULT gen_random_uuid() PRIMARY KEY, restaurant_id uuid, branch_id uuid, ingredient_id text REFERENCES ingredients,
 batch_number text, received_quantity numeric CHECK(received_quantity>0), remaining_quantity numeric CHECK(remaining_quantity>=0),
 unit text, unit_cost numeric DEFAULT 0, expiry_date date, received_date date DEFAULT CURRENT_DATE,
 status text DEFAULT 'active' CHECK(status IN ('active','depleted','expired','quarantined','recalled')),
 created_by text, updated_at timestamptz
);
CREATE TABLE unit_conversions (id uuid DEFAULT gen_random_uuid(), restaurant_id uuid, ingredient_id text,
 from_unit text, to_unit text, factor numeric, is_active boolean DEFAULT true);
CREATE TABLE recipes (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), restaurant_id uuid, item_id integer,
 is_active boolean DEFAULT true, yield_quantity numeric DEFAULT 1);
CREATE TABLE recipe_ingredients (recipe_id uuid REFERENCES recipes, ingredient_id text, quantity_required numeric, unit text, waste_percent numeric DEFAULT 0);
CREATE TABLE item_ingredients (item_id integer, ingredient_id text, quantity_used numeric, unit text);
CREATE SEQUENCE po_number_seq;
CREATE TABLE purchase_orders (
 id serial PRIMARY KEY, user_id text, branch_id uuid, po_number text, source_type text, source_id text, source_name text,
 supplier_name text, supplier_contact text, status text DEFAULT 'draft', notes text,
 expected_delivery_date date, delivery_time time, sub_total numeric, delivery_charges numeric, total_amount numeric,
 payment_status text, approval_status text DEFAULT 'draft', approved_by text, approved_at timestamptz, requested_by text,
 rejected_reason text, received_at timestamptz, updated_at timestamptz
);
CREATE TABLE purchase_order_items (id serial PRIMARY KEY, purchase_order_id integer REFERENCES purchase_orders,
 ingredient_id text REFERENCES ingredients, ingredient_name text, quantity_ordered numeric, quantity numeric,
 unit text, unit_cost numeric, unit_price numeric, amount numeric);
CREATE TABLE inventory_sales (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, restaurant_id uuid, branch_id uuid,
 sale_number text DEFAULT 'SALE-TEST', buyer_name text, buyer_gst text, buyer_contact text, buyer_address text,
 sub_total numeric, tax_amount numeric, total_amount numeric, terms text, notes text, created_by text);
CREATE TABLE inventory_sale_items (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, sale_id uuid REFERENCES inventory_sales,
 ingredient_id text, quantity numeric, unit text, unit_price numeric, tax_percent numeric, line_total numeric);
CREATE TABLE inventory_conversions (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, restaurant_id uuid, branch_id uuid,
 conversion_recipe_id uuid, output_ingredient_id text REFERENCES ingredients, produced_quantity numeric,
 output_unit text, notes text, created_by text);
CREATE TABLE stock_transfers (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, restaurant_id uuid,
 from_branch_id uuid, to_branch_id uuid, status text DEFAULT 'draft', requested_by text, notes text,
 received_by text, shipped_at timestamptz, received_at timestamptz);
CREATE TABLE stock_transfer_items (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, transfer_id uuid REFERENCES stock_transfers,
 ingredient_id text REFERENCES ingredients, quantity_sent numeric CHECK(quantity_sent>0), quantity_received numeric, unit text);
CREATE TABLE inventory_counts (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, restaurant_id uuid, branch_id uuid,
 count_number text, status text, approved_by text, approved_at timestamptz, completed_at timestamptz, completed_by text);
CREATE TABLE inventory_count_items (id uuid DEFAULT gen_random_uuid(), count_id uuid REFERENCES inventory_counts,
 ingredient_id text, expected_qty numeric, counted_qty numeric, unit text, unit_cost numeric,
 variance numeric GENERATED ALWAYS AS (COALESCE(counted_qty,0)-expected_qty) STORED,
 counted_by text, counted_at timestamptz);
