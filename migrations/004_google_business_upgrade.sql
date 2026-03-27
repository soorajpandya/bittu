-- ════════════════════════════════════════════════════════════════
-- BITTU — Google Business Profile Upgrade Migration
-- Adds: google_locations, google_reviews, google_posts,
--        google_insights_daily, google_oauth_states
-- Alters: google_connections (adds last_sync columns)
-- ════════════════════════════════════════════════════════════════

-- ── Extend google_connections with sync metadata ──
ALTER TABLE google_connections
    ADD COLUMN IF NOT EXISTS last_locations_sync TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_reviews_sync   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_insights_sync  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_posts_sync     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS sync_error          TEXT;

-- ── OAuth state store (one-time use, with TTL enforcement) ──
CREATE TABLE IF NOT EXISTS google_oauth_states (
    state           TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    restaurant_id   TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    redirect_uri    TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    used            BOOLEAN DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_oauth_state_expires ON google_oauth_states(expires_at);

-- ── Cached locations ──
CREATE TABLE IF NOT EXISTS google_locations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   TEXT NOT NULL,
    account_id      TEXT NOT NULL,
    location_id     TEXT NOT NULL,
    location_name   TEXT,
    address         JSONB,
    phone           TEXT,
    website_uri     TEXT,
    raw_data        JSONB,
    synced_at       TIMESTAMPTZ DEFAULT now(),

    UNIQUE (restaurant_id, account_id, location_id)
);

CREATE INDEX IF NOT EXISTS idx_google_loc_restaurant ON google_locations(restaurant_id);

-- ── Cached reviews ──
CREATE TABLE IF NOT EXISTS google_reviews (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   TEXT NOT NULL,
    review_id       TEXT NOT NULL,
    reviewer_name   TEXT,
    star_rating     TEXT,
    comment         TEXT,
    create_time     TIMESTAMPTZ,
    update_time     TIMESTAMPTZ,
    reply_comment   TEXT,
    reply_time      TIMESTAMPTZ,
    raw_data        JSONB,
    synced_at       TIMESTAMPTZ DEFAULT now(),

    UNIQUE (restaurant_id, review_id)
);

CREATE INDEX IF NOT EXISTS idx_google_rev_restaurant ON google_reviews(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_google_rev_created ON google_reviews(create_time DESC);

-- ── Cached posts ──
CREATE TABLE IF NOT EXISTS google_posts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   TEXT NOT NULL,
    post_id         TEXT NOT NULL,
    topic_type      TEXT,
    summary         TEXT,
    action_type     TEXT,
    action_url      TEXT,
    image_url       TEXT,
    state           TEXT,   -- LIVE, REJECTED, etc.
    create_time     TIMESTAMPTZ,
    update_time     TIMESTAMPTZ,
    raw_data        JSONB,
    synced_at       TIMESTAMPTZ DEFAULT now(),

    UNIQUE (restaurant_id, post_id)
);

CREATE INDEX IF NOT EXISTS idx_google_post_restaurant ON google_posts(restaurant_id);

-- ── Daily insights (aggregated) ──
CREATE TABLE IF NOT EXISTS google_insights_daily (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   TEXT NOT NULL,
    metric_date     DATE NOT NULL,
    metric_name     TEXT NOT NULL,
    metric_value    INTEGER DEFAULT 0,
    synced_at       TIMESTAMPTZ DEFAULT now(),

    UNIQUE (restaurant_id, metric_date, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_google_insights_restaurant ON google_insights_daily(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_google_insights_date ON google_insights_daily(metric_date DESC);
CREATE INDEX IF NOT EXISTS idx_google_insights_lookup ON google_insights_daily(restaurant_id, metric_date, metric_name);

-- ── Cleanup job: purge expired OAuth states ──
-- Run periodically: DELETE FROM google_oauth_states WHERE expires_at < now();
