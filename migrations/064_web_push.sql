-- Web Push (VAPID) — customer-facing browser notifications for QR waitlist
--
-- Stores a singleton VAPID keypair (auto-generated on first use) and one row
-- per browser subscription. Subscriptions are tied to waitlist entries, not
-- user accounts, since QR customers are anonymous.

CREATE TABLE IF NOT EXISTS web_push_vapid_keys (
  id           SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  public_key   TEXT NOT NULL,    -- URL-safe base64 raw EC point (P-256)
  private_pem  TEXT NOT NULL,    -- PEM-encoded PKCS#8 private key
  created_at   TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS web_push_subscriptions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id       UUID NOT NULL REFERENCES waitlist_entries(id) ON DELETE CASCADE,
  restaurant_id  UUID NOT NULL,
  endpoint       TEXT NOT NULL,
  p256dh         TEXT NOT NULL,
  auth           TEXT NOT NULL,
  user_agent     TEXT,
  created_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
  UNIQUE (entry_id, endpoint)
);

CREATE INDEX IF NOT EXISTS idx_web_push_subscriptions_entry
  ON web_push_subscriptions(entry_id);
CREATE INDEX IF NOT EXISTS idx_web_push_subscriptions_restaurant
  ON web_push_subscriptions(restaurant_id);
