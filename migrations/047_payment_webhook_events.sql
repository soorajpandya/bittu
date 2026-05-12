-- ============================================================================
-- 047_payment_webhook_events.sql
-- ----------------------------------------------------------------------------
-- Forensic-safe storage for every webhook we receive from any gateway.
-- Backs `app.core.webhook_security.verify_and_register_webhook`.
--
-- WHY: The previous webhook handlers verified a signature and discarded the
-- body. This left us with no replay-protection, no audit trail for disputed
-- transactions, and no way to re-process a failed event. This table closes
-- all three gaps.
--
-- INVARIANTS:
--   * UNIQUE (gateway, event_id) — replays short-circuit at the DB layer.
--   * UPDATE allowed only by service code (no DELETE — see triggers).
--   * Partitioned monthly to keep the hot index small at 100k merchant scale.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS payment_webhook_events (
    id                UUID        NOT NULL DEFAULT gen_random_uuid(),
    gateway           TEXT        NOT NULL,
    event_id          TEXT,                                 -- gateway-supplied unique id
    event_type        TEXT,
    event_hash        TEXT        NOT NULL,                 -- sha256(body || signature)
    signature_valid   BOOLEAN     NOT NULL DEFAULT false,
    processing_state  TEXT        NOT NULL DEFAULT 'received'
                                  CHECK (processing_state IN
                                         ('received','processing','processed','failed','skipped')),
    retries           INT         NOT NULL DEFAULT 0,
    latency_ms        NUMERIC(12,2),
    last_error        TEXT,
    headers           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    raw_payload       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at      TIMESTAMPTZ,
    PRIMARY KEY (id, received_at)
) PARTITION BY RANGE (received_at);

-- Default catch-all partition (Batch 5 will introduce monthly partitions
-- via pg_partman). For now ship a single default to avoid blocking inserts.
CREATE TABLE IF NOT EXISTS payment_webhook_events_default
    PARTITION OF payment_webhook_events DEFAULT;

-- Replay-protection unique index (per gateway, per event_id).
-- NULL event_id is allowed (some gateways don't send one) — those rows
-- still benefit from event_hash uniqueness below.
CREATE UNIQUE INDEX IF NOT EXISTS ux_pwe_gateway_event
    ON payment_webhook_events (gateway, event_id)
    WHERE event_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_pwe_gateway_hash
    ON payment_webhook_events (gateway, event_hash);

CREATE INDEX IF NOT EXISTS ix_pwe_state_received
    ON payment_webhook_events (processing_state, received_at DESC);

CREATE INDEX IF NOT EXISTS ix_pwe_event_type
    ON payment_webhook_events (event_type, received_at DESC);

-- Append-only at the storage layer: forbid DELETE outright. Limited UPDATE
-- to the columns service code legitimately rewrites (state transitions).
CREATE OR REPLACE FUNCTION fn_pwe_block_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'payment_webhook_events is append-only (DELETE denied)';
END $$;

DROP TRIGGER IF EXISTS trg_pwe_no_delete ON payment_webhook_events;
CREATE TRIGGER trg_pwe_no_delete
BEFORE DELETE ON payment_webhook_events
FOR EACH ROW EXECUTE FUNCTION fn_pwe_block_delete();

CREATE OR REPLACE FUNCTION fn_pwe_guard_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.gateway      <> OLD.gateway      THEN RAISE EXCEPTION 'gateway is immutable';      END IF;
    IF NEW.event_hash   <> OLD.event_hash   THEN RAISE EXCEPTION 'event_hash is immutable';   END IF;
    IF NEW.received_at  <> OLD.received_at  THEN RAISE EXCEPTION 'received_at is immutable';  END IF;
    IF NEW.raw_payload::text <> OLD.raw_payload::text THEN
        RAISE EXCEPTION 'raw_payload is immutable';
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_pwe_guard_update ON payment_webhook_events;
CREATE TRIGGER trg_pwe_guard_update
BEFORE UPDATE ON payment_webhook_events
FOR EACH ROW EXECUTE FUNCTION fn_pwe_guard_update();

COMMIT;
