-- Migration 012: Dine-in session engine hardening (additive only)
-- Adds multi-user session tracking, partial payments, and cached session totals.

-- 1) Extend dine_in_sessions with session-level cache fields
ALTER TABLE dine_in_sessions
    ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS total_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS remaining_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS active_users_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS created_by VARCHAR(20) DEFAULT 'qr';

-- Optional compatibility field for merge chains.
ALTER TABLE dine_in_sessions
    ADD COLUMN IF NOT EXISTS merged_into_session_id UUID REFERENCES dine_in_sessions(id) ON DELETE SET NULL;

-- 2) Session users (qr/staff participants)
CREATE TABLE IF NOT EXISTS table_session_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES dine_in_sessions(id) ON DELETE CASCADE,
    user_type VARCHAR(20) NOT NULL CHECK (user_type IN ('qr', 'staff')),
    name VARCHAR(120),
    device_id VARCHAR(120),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active BOOLEAN NOT NULL DEFAULT true
);

CREATE INDEX IF NOT EXISTS idx_table_session_users_session
    ON table_session_users(session_id);

CREATE INDEX IF NOT EXISTS idx_table_session_users_device
    ON table_session_users(session_id, device_id)
    WHERE device_id IS NOT NULL;

-- 3) Session payments (supports partial + multi-method settlement)
CREATE TABLE IF NOT EXISTS table_session_payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES dine_in_sessions(id) ON DELETE CASCADE,
    order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
    amount NUMERIC(12,2) NOT NULL CHECK (amount > 0),
    payment_method VARCHAR(50) NOT NULL,
    transaction_ref VARCHAR(120),
    paid_by VARCHAR(120),
    notes TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_table_session_payments_session
    ON table_session_payments(session_id, created_at DESC);

-- 4) Align cart table foreign key with dine_in_sessions (if legacy points to table_sessions)
DO $$
DECLARE
    fk_name text;
BEGIN
    SELECT tc.constraint_name
      INTO fk_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
     WHERE tc.table_name = 'table_session_carts'
       AND tc.constraint_type = 'FOREIGN KEY'
       AND kcu.column_name = 'session_id'
     LIMIT 1;

    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE table_session_carts DROP CONSTRAINT IF EXISTS %I', fk_name);
    END IF;

    ALTER TABLE table_session_carts
        ADD CONSTRAINT table_session_carts_session_id_fkey
        FOREIGN KEY (session_id) REFERENCES dine_in_sessions(id) ON DELETE CASCADE;
END $$;

DO $$
DECLARE
    fk_name text;
BEGIN
    SELECT tc.constraint_name
      INTO fk_name
      FROM information_schema.table_constraints tc
      JOIN information_schema.key_column_usage kcu
        ON tc.constraint_name = kcu.constraint_name
     WHERE tc.table_name = 'session_orders'
       AND tc.constraint_type = 'FOREIGN KEY'
       AND kcu.column_name = 'session_id'
     LIMIT 1;

    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE session_orders DROP CONSTRAINT IF EXISTS %I', fk_name);
    END IF;

    ALTER TABLE session_orders
        ADD CONSTRAINT session_orders_session_id_fkey
        FOREIGN KEY (session_id) REFERENCES dine_in_sessions(id) ON DELETE CASCADE;
END $$;

-- 5) One active session per table (strict rule)
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_dinein_session_per_table
    ON dine_in_sessions(table_id)
    WHERE status = 'active';

-- 6) Performance indexes
CREATE INDEX IF NOT EXISTS idx_dine_in_sessions_table_status
    ON dine_in_sessions(table_id, status);

CREATE INDEX IF NOT EXISTS idx_dine_in_sessions_restaurant_status
    ON dine_in_sessions(restaurant_id, status);
