-- Migration 009: Smart Waitlist System
-- Hybrid waitlist with staff + QR entry, best-fit table allocation

-- ── waitlist_entries table ──
CREATE TABLE IF NOT EXISTS waitlist_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL,
    branch_id       UUID,
    user_id         TEXT NOT NULL,                          -- restaurant owner
    customer_name   VARCHAR(100) NOT NULL,
    phone           VARCHAR(20),
    party_size      INTEGER NOT NULL DEFAULT 1,
    source          VARCHAR(10) NOT NULL DEFAULT 'staff',   -- staff | qr
    status          VARCHAR(20) NOT NULL DEFAULT 'waiting', -- waiting | notified | seated | skipped | cancelled
    position        INTEGER NOT NULL,                       -- queue position at time of entry
    estimated_wait_minutes INTEGER,
    notes           TEXT,
    -- notification tracking
    notified_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,                            -- deadline to arrive after notification
    seated_at       TIMESTAMPTZ,
    -- table assignment
    assigned_table_id UUID REFERENCES restaurant_tables(id) ON DELETE SET NULL,
    -- timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_waitlist_restaurant_status
    ON waitlist_entries (restaurant_id, status, position);

CREATE INDEX IF NOT EXISTS idx_waitlist_phone
    ON waitlist_entries (restaurant_id, phone, status);

CREATE INDEX IF NOT EXISTS idx_waitlist_created
    ON waitlist_entries (restaurant_id, created_at DESC);

-- ── waitlist_settings table (per-restaurant config) ──
CREATE TABLE IF NOT EXISTS waitlist_settings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL,
    user_id         TEXT NOT NULL,
    -- timing
    notify_expiry_minutes   INTEGER NOT NULL DEFAULT 5,     -- minutes to arrive after notification
    avg_turnover_minutes    INTEGER NOT NULL DEFAULT 30,    -- avg time per table sitting
    -- features
    sms_enabled             BOOLEAN NOT NULL DEFAULT false,
    whatsapp_enabled        BOOLEAN NOT NULL DEFAULT false,
    display_screen_enabled  BOOLEAN NOT NULL DEFAULT false,
    qr_entry_enabled        BOOLEAN NOT NULL DEFAULT true,
    auto_notify             BOOLEAN NOT NULL DEFAULT true,  -- auto-notify best-fit on table free
    best_fit_enabled        BOOLEAN NOT NULL DEFAULT true,  -- smart allocation vs FIFO
    -- display
    display_message         TEXT DEFAULT 'Welcome! Please wait for your table.',
    -- timestamps
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(restaurant_id)
);

-- ── waitlist_history (for analytics / audit) ──
CREATE TABLE IF NOT EXISTS waitlist_history (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   UUID NOT NULL,
    waitlist_entry_id UUID NOT NULL,
    action          VARCHAR(30) NOT NULL,  -- added | notified | seated | skipped | cancelled | reordered
    details         JSONB,
    performed_by    TEXT,                  -- user_id of staff, or 'customer'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_waitlist_history_entry
    ON waitlist_history (waitlist_entry_id, created_at);

CREATE INDEX IF NOT EXISTS idx_waitlist_history_restaurant
    ON waitlist_history (restaurant_id, created_at DESC);

-- ── Auto-update updated_at triggers ──
CREATE OR REPLACE FUNCTION update_waitlist_entries_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_waitlist_entries_updated_at ON waitlist_entries;
CREATE TRIGGER trg_waitlist_entries_updated_at
    BEFORE UPDATE ON waitlist_entries
    FOR EACH ROW EXECUTE FUNCTION update_waitlist_entries_updated_at();

CREATE OR REPLACE FUNCTION update_waitlist_settings_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_waitlist_settings_updated_at ON waitlist_settings;
CREATE TRIGGER trg_waitlist_settings_updated_at
    BEFORE UPDATE ON waitlist_settings
    FOR EACH ROW EXECUTE FUNCTION update_waitlist_settings_updated_at();

-- ── RLS ──
ALTER TABLE waitlist_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE waitlist_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE waitlist_history ENABLE ROW LEVEL SECURITY;

-- Read/write by owner
CREATE POLICY waitlist_entries_tenant ON waitlist_entries
    USING (user_id = auth.uid()::text);
CREATE POLICY waitlist_settings_tenant ON waitlist_settings
    USING (user_id = auth.uid()::text);
CREATE POLICY waitlist_history_tenant ON waitlist_history
    USING (restaurant_id IN (SELECT restaurant_id FROM waitlist_entries WHERE user_id = auth.uid()::text));

-- Service role full access
CREATE POLICY waitlist_entries_service ON waitlist_entries
    USING (true) WITH CHECK (true);
CREATE POLICY waitlist_settings_service ON waitlist_settings
    USING (true) WITH CHECK (true);
CREATE POLICY waitlist_history_service ON waitlist_history
    USING (true) WITH CHECK (true);
