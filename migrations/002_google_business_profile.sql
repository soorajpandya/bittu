-- ════════════════════════════════════════════════════════════════
-- BITTU — Google Business Profile Integration Schema
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS google_connections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    restaurant_id   TEXT NOT NULL,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,
    token_expiry    TIMESTAMPTZ NOT NULL,
    account_id      TEXT,            -- Google Business account ID
    location_id     TEXT,            -- Google Business location ID
    location_name   TEXT,            -- Human-readable location name
    scopes          TEXT DEFAULT 'https://www.googleapis.com/auth/business.manage',
    is_active       BOOLEAN DEFAULT true,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),

    UNIQUE (user_id, restaurant_id)
);

-- Index for quick lookups
CREATE INDEX IF NOT EXISTS idx_google_conn_user ON google_connections(user_id);
CREATE INDEX IF NOT EXISTS idx_google_conn_restaurant ON google_connections(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_google_conn_active ON google_connections(is_active) WHERE is_active = true;
