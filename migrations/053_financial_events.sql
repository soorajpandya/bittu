-- ============================================================================
-- 053_financial_events.sql
-- ----------------------------------------------------------------------------
-- The IMMUTABLE FINANCIAL LAYER.
--
-- Operational tables (orders, payments, refunds, settlements, disputes...)
-- mutate. The financial truth must NEVER mutate. This event store is the
-- canonical, append-only log of every money-affecting event the platform
-- produces, and is the source from which financial state can be replayed.
--
-- Schema notes
-- ------------
--   * Hash-chained per (aggregate_type, aggregate_id):
--       row_hash = sha256(prev_hash || canonical_payload || event_id)
--     A new row's prev_hash MUST equal the prior row's row_hash for the
--     same aggregate stream. fn_append_financial_event enforces this.
--
--   * BIGSERIAL `seq` is a global ordering. Per-stream ordering is
--     enforced by stream_version (1, 2, 3, ...).
--
--   * Partitioned monthly by created_at to stay performant.
--
--   * APPEND-ONLY: UPDATE & DELETE rejected by trigger. The only legal
--     write path is fn_append_financial_event (which itself takes an
--     advisory lock per stream to serialise hash chaining).
--
-- Replay
-- ------
-- Given an aggregate_type + aggregate_id, a worker can stream events in
-- stream_version order and re-derive any operational state:
--   * settlement_lifecycle    from settlement.* events
--   * merchant_balance_history from ledger.* events
--   * escrow_movements         from escrow.* events
--   * reconciliation_history   from recon.* events
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS financial_events (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    seq                 BIGSERIAL,                                -- global order

    aggregate_type      TEXT         NOT NULL,                    -- 'payment'|'settlement'|'escrow'|'merchant_ledger'|'recon'|'dispute'
    aggregate_id        UUID         NOT NULL,
    stream_version      INT          NOT NULL,                    -- 1,2,3,... per stream

    event_type          TEXT         NOT NULL,                    -- e.g. 'payment.captured'
    event_version       INT          NOT NULL DEFAULT 1,          -- payload schema version

    -- Canonical, frozen payload — never re-serialised.
    payload             JSONB        NOT NULL,
    payload_canonical   TEXT         NOT NULL,                    -- jsonb_canonical text used for hash

    -- Hash chain
    prev_hash           TEXT,
    row_hash            TEXT         NOT NULL,

    -- Provenance
    correlation_id      TEXT,                                     -- request_id from middleware
    causation_id        UUID,                                     -- prior event that caused this one
    actor_type          TEXT,                                     -- 'user'|'system'|'worker'|'gateway'
    actor_id            UUID,
    occurred_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (id, created_at),
    CONSTRAINT chk_fe_stream_version CHECK (stream_version >= 1)
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS financial_events_default
    PARTITION OF financial_events DEFAULT;

CREATE INDEX IF NOT EXISTS ix_fe_stream
    ON financial_events (aggregate_type, aggregate_id, stream_version);

CREATE INDEX IF NOT EXISTS ix_fe_type_created
    ON financial_events (event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_fe_correlation
    ON financial_events (correlation_id) WHERE correlation_id IS NOT NULL;

-- Stream-version uniqueness companion (PK can't enforce across partitions).
CREATE TABLE IF NOT EXISTS financial_event_stream_index (
    aggregate_type   TEXT NOT NULL,
    aggregate_id     UUID NOT NULL,
    stream_version   INT  NOT NULL,
    event_id         UUID NOT NULL,
    row_hash         TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (aggregate_type, aggregate_id, stream_version)
);


-- Append-only triggers ------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_financial_events_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'financial_events is append-only (DELETE denied)';
    END IF;
    RAISE EXCEPTION 'financial_events is append-only (UPDATE denied)';
END $$;

DROP TRIGGER IF EXISTS trg_fe_no_update ON financial_events;
DROP TRIGGER IF EXISTS trg_fe_no_delete ON financial_events;
CREATE TRIGGER trg_fe_no_update BEFORE UPDATE ON financial_events
FOR EACH ROW EXECUTE FUNCTION fn_financial_events_block_mutation();
CREATE TRIGGER trg_fe_no_delete BEFORE DELETE ON financial_events
FOR EACH ROW EXECUTE FUNCTION fn_financial_events_block_mutation();


-- Helper: deterministic JSONB serialisation (sorted keys) -------------------
-- We rely on jsonb's canonical text form (PG sorts keys alphabetically and
-- removes duplicates/whitespace), so jsonb::text IS canonical enough for
-- a sha256 chain. We capture it into payload_canonical at insert time so
-- replays are bit-identical.
CREATE OR REPLACE FUNCTION fn_financial_event_canonical(p_payload JSONB)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
    SELECT (p_payload)::text
$$;


-- Single legal write path ---------------------------------------------------
-- Takes a per-stream advisory lock so the prev_hash → row_hash chain is
-- strictly serial within an aggregate, while not blocking other streams.
CREATE OR REPLACE FUNCTION fn_append_financial_event(
    p_aggregate_type TEXT,
    p_aggregate_id   UUID,
    p_event_type     TEXT,
    p_payload        JSONB,
    p_event_version  INT     DEFAULT 1,
    p_correlation_id TEXT    DEFAULT NULL,
    p_causation_id   UUID    DEFAULT NULL,
    p_actor_type     TEXT    DEFAULT 'system',
    p_actor_id       UUID    DEFAULT NULL,
    p_occurred_at    TIMESTAMPTZ DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_lock_key   BIGINT;
    v_prev_hash  TEXT;
    v_prev_ver   INT;
    v_canonical  TEXT;
    v_row_hash   TEXT;
    v_id         UUID := gen_random_uuid();
    v_ver        INT;
BEGIN
    -- Stream-scoped advisory lock: hash(aggregate_type || aggregate_id::text)
    v_lock_key := ('x' || substr(md5(p_aggregate_type || p_aggregate_id::text), 1, 16))::bit(64)::bigint;
    PERFORM pg_advisory_xact_lock(v_lock_key);

    SELECT stream_version, row_hash
      INTO v_prev_ver, v_prev_hash
      FROM financial_event_stream_index
     WHERE aggregate_type = p_aggregate_type
       AND aggregate_id   = p_aggregate_id
     ORDER BY stream_version DESC
     LIMIT 1;

    v_ver := COALESCE(v_prev_ver, 0) + 1;
    v_canonical := fn_financial_event_canonical(p_payload);
    v_row_hash := encode(
        digest(COALESCE(v_prev_hash, '') || v_canonical || v_id::text || v_ver::text, 'sha256'),
        'hex'
    );

    INSERT INTO financial_events(
        id, aggregate_type, aggregate_id, stream_version,
        event_type, event_version, payload, payload_canonical,
        prev_hash, row_hash, correlation_id, causation_id,
        actor_type, actor_id, occurred_at)
    VALUES (
        v_id, p_aggregate_type, p_aggregate_id, v_ver,
        p_event_type, COALESCE(p_event_version, 1), p_payload, v_canonical,
        v_prev_hash, v_row_hash, p_correlation_id, p_causation_id,
        COALESCE(p_actor_type, 'system'), p_actor_id,
        COALESCE(p_occurred_at, NOW())
    );

    INSERT INTO financial_event_stream_index(
        aggregate_type, aggregate_id, stream_version, event_id, row_hash)
    VALUES (p_aggregate_type, p_aggregate_id, v_ver, v_id, v_row_hash);

    RETURN jsonb_build_object(
        'event_id',       v_id,
        'stream_version', v_ver,
        'row_hash',       v_row_hash,
        'prev_hash',      v_prev_hash
    );
END $$;


-- Verification: walk a stream and confirm the hash chain ---------------------
CREATE OR REPLACE FUNCTION fn_verify_financial_stream(
    p_aggregate_type TEXT,
    p_aggregate_id   UUID
) RETURNS TABLE(stream_version INT, ok BOOLEAN, expected_hash TEXT, actual_hash TEXT)
LANGUAGE plpgsql AS $$
DECLARE
    r RECORD;
    v_prev TEXT := NULL;
    v_expected TEXT;
BEGIN
    FOR r IN
        SELECT fe.stream_version, fe.payload_canonical, fe.id, fe.row_hash, fe.prev_hash
          FROM financial_events fe
         WHERE fe.aggregate_type = p_aggregate_type
           AND fe.aggregate_id   = p_aggregate_id
         ORDER BY fe.stream_version
    LOOP
        v_expected := encode(
            digest(COALESCE(v_prev, '') || r.payload_canonical || r.id::text || r.stream_version::text, 'sha256'),
            'hex'
        );
        stream_version := r.stream_version;
        expected_hash  := v_expected;
        actual_hash    := r.row_hash;
        ok             := (v_expected = r.row_hash) AND (COALESCE(r.prev_hash,'') = COALESCE(v_prev,''));
        RETURN NEXT;
        v_prev := r.row_hash;
    END LOOP;
END $$;

COMMIT;
