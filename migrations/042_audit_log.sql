-- ╔════════════════════════════════════════════════════════════════════════╗
-- ║ Migration 042 — Phase 6: Audit & Compliance                           ║
-- ║                                                                        ║
-- ║ Append-only, hash-chained audit log of sensitive actions across the   ║
-- ║ platform. Tamper detection via SHA-256 chain (row_hash links to       ║
-- ║ prev_hash). Append-only enforced by BEFORE UPDATE/DELETE trigger      ║
-- ║ raising P0002 on any mutation (matches the merchant_ledger pattern).  ║
-- ║                                                                        ║
-- ║ Schema:                                                                ║
-- ║   • audit_events            — the chain itself                         ║
-- ║                                                                        ║
-- ║ Functions:                                                             ║
-- ║   • fn_append_audit_event   — only legal write path                    ║
-- ║   • fn_verify_audit_chain   — recomputes hashes, returns first bad    ║
-- ║                                                                        ║
-- ║ Permissions:                                                           ║
-- ║   • audit.read              — read own merchant audit events           ║
-- ║   • audit.read.all          — admin: read across merchants             ║
-- ║   • audit.verify            — admin: run chain verification            ║
-- ╚════════════════════════════════════════════════════════════════════════╝

BEGIN;

-- pgcrypto for digest()/sha256
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ── 1. audit_events ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_events (
    -- Sequential id is the chain ordering. BIGSERIAL guarantees monotonic
    -- assignment under the per-row advisory lock taken in the writer fn.
    id              BIGSERIAL PRIMARY KEY,
    event_uuid      UUID        NOT NULL UNIQUE DEFAULT gen_random_uuid(),

    -- Scope (nullable for platform-wide events)
    merchant_id     UUID,

    -- Actor
    actor_type      TEXT        NOT NULL,           -- user|admin|system|cron
    actor_user_id   UUID,
    actor_label     TEXT,                           -- email or display name snapshot

    -- Event
    action          TEXT        NOT NULL,           -- e.g. invoice.issued
    resource_type   TEXT,                           -- e.g. tax_invoice
    resource_id     TEXT,                           -- stringified PK

    payload         JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Request metadata (best-effort)
    ip_address      INET,
    user_agent      TEXT,
    request_id      TEXT,

    -- Hash chain
    prev_hash       TEXT,                           -- NULL for genesis row
    row_hash        TEXT        NOT NULL,           -- sha256 hex over canonical fields

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_audit_actor_type
        CHECK (actor_type IN ('user', 'admin', 'system', 'cron')),
    CONSTRAINT chk_audit_action_nonempty
        CHECK (length(action) > 0),
    CONSTRAINT chk_audit_row_hash_len
        CHECK (length(row_hash) = 64),
    CONSTRAINT chk_audit_prev_hash_len
        CHECK (prev_hash IS NULL OR length(prev_hash) = 64)
);

CREATE INDEX IF NOT EXISTS idx_audit_events_merchant_created
    ON audit_events (merchant_id, created_at DESC)
 WHERE merchant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_events_actor
    ON audit_events (actor_user_id, created_at DESC)
 WHERE actor_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_events_action_created
    ON audit_events (action, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_resource
    ON audit_events (resource_type, resource_id)
 WHERE resource_type IS NOT NULL;


-- ── 2. Append-only enforcement (P0002 on UPDATE/DELETE) ─────────────────
CREATE OR REPLACE FUNCTION fn_audit_events_append_only()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'audit_events is append-only (operation=% on id=%)',
        TG_OP, COALESCE(OLD.id, NEW.id)
        USING ERRCODE = 'P0002';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_events_no_update ON audit_events;
CREATE TRIGGER trg_audit_events_no_update
    BEFORE UPDATE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION fn_audit_events_append_only();

DROP TRIGGER IF EXISTS trg_audit_events_no_delete ON audit_events;
CREATE TRIGGER trg_audit_events_no_delete
    BEFORE DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION fn_audit_events_append_only();


-- ── 3. fn_append_audit_event — only legal write path ────────────────────
--
-- Computes row_hash deterministically from prev_hash + canonical payload.
-- Holds an advisory lock (key derived from a constant tag) for the duration
-- of the read-of-tail + insert so concurrent writers serialise on the chain
-- tip and prev_hash always points at the truly previous row.
--
-- Canonical hash input (newline-separated, NULL → empty string):
--    prev_hash | event_uuid | merchant_id | actor_type | actor_user_id
--    action | resource_type | resource_id | payload(jsonb canonical text)
--    ip_address | request_id | created_at
--
-- jsonb canonicalisation: cast through ::jsonb then text — Postgres jsonb
-- already strips whitespace and orders keys, so identical logical payloads
-- produce identical hashes regardless of input formatting.

CREATE OR REPLACE FUNCTION fn_append_audit_event(
    p_merchant_id     UUID,
    p_actor_type      TEXT,
    p_actor_user_id   UUID,
    p_actor_label     TEXT,
    p_action          TEXT,
    p_resource_type   TEXT,
    p_resource_id     TEXT,
    p_payload         JSONB,
    p_ip_address      INET,
    p_user_agent      TEXT,
    p_request_id      TEXT
) RETURNS audit_events LANGUAGE plpgsql AS $$
DECLARE
    v_lock_key   BIGINT := hashtext('audit_events_chain');
    v_prev_hash  TEXT;
    v_event_uuid UUID := gen_random_uuid();
    v_now        TIMESTAMPTZ := now();
    v_canonical  TEXT;
    v_row_hash   TEXT;
    v_row        audit_events;
    v_payload    JSONB := COALESCE(p_payload, '{}'::jsonb);
BEGIN
    IF p_actor_type NOT IN ('user', 'admin', 'system', 'cron') THEN
        RAISE EXCEPTION 'invalid actor_type: %', p_actor_type
            USING ERRCODE = '22023';
    END IF;
    IF p_action IS NULL OR length(p_action) = 0 THEN
        RAISE EXCEPTION 'action is required' USING ERRCODE = '22023';
    END IF;

    -- Serialise concurrent appenders so prev_hash points at the real tail.
    PERFORM pg_advisory_xact_lock(v_lock_key);

    SELECT row_hash INTO v_prev_hash
      FROM audit_events
     ORDER BY id DESC
     LIMIT 1;

    v_canonical :=
        COALESCE(v_prev_hash,             '') || E'\n' ||
        v_event_uuid::text                     || E'\n' ||
        COALESCE(p_merchant_id::text,     '') || E'\n' ||
        p_actor_type                           || E'\n' ||
        COALESCE(p_actor_user_id::text,   '') || E'\n' ||
        p_action                               || E'\n' ||
        COALESCE(p_resource_type,         '') || E'\n' ||
        COALESCE(p_resource_id,           '') || E'\n' ||
        v_payload::text                        || E'\n' ||
        COALESCE(host(p_ip_address),      '') || E'\n' ||
        COALESCE(p_request_id,            '') || E'\n' ||
        v_now::text;

    v_row_hash := encode(digest(v_canonical, 'sha256'), 'hex');

    INSERT INTO audit_events (
        event_uuid, merchant_id, actor_type, actor_user_id, actor_label,
        action, resource_type, resource_id, payload,
        ip_address, user_agent, request_id,
        prev_hash, row_hash, created_at
    ) VALUES (
        v_event_uuid, p_merchant_id, p_actor_type, p_actor_user_id, p_actor_label,
        p_action, p_resource_type, p_resource_id, v_payload,
        p_ip_address, p_user_agent, p_request_id,
        v_prev_hash, v_row_hash, v_now
    )
    RETURNING * INTO v_row;

    RETURN v_row;
END;
$$;


-- ── 4. fn_verify_audit_chain — recomputes hashes ────────────────────────
--
-- Walks rows in id order between [start_id, end_id] (NULL = open ended) and
-- recomputes each row's row_hash from its stored fields + the previous
-- row's row_hash. Returns NULL on success; returns the first offending row
-- on first mismatch.
--
-- Result columns:
--    bad_id, bad_event_uuid, expected_hash, stored_hash
--
-- Cost: O(n) sequential read of the slice. Intended for periodic admin
-- verification jobs, not per-request.

CREATE OR REPLACE FUNCTION fn_verify_audit_chain(
    p_start_id BIGINT DEFAULT NULL,
    p_end_id   BIGINT DEFAULT NULL
) RETURNS TABLE (
    bad_id         BIGINT,
    bad_event_uuid UUID,
    expected_hash  TEXT,
    stored_hash    TEXT,
    expected_prev  TEXT,
    stored_prev    TEXT
) LANGUAGE plpgsql STABLE AS $$
DECLARE
    r           RECORD;
    v_prev_hash TEXT;
    v_canonical TEXT;
    v_expected  TEXT;
    v_first     BOOLEAN := TRUE;
BEGIN
    -- Seed v_prev_hash from the row immediately before the range so we
    -- can validate that the first row's prev_hash links correctly.
    IF p_start_id IS NOT NULL THEN
        SELECT row_hash INTO v_prev_hash
          FROM audit_events
         WHERE id < p_start_id
         ORDER BY id DESC
         LIMIT 1;
    END IF;

    FOR r IN
        SELECT *
          FROM audit_events
         WHERE (p_start_id IS NULL OR id >= p_start_id)
           AND (p_end_id   IS NULL OR id <= p_end_id)
         ORDER BY id ASC
    LOOP
        -- prev_hash linkage check (skip for genesis row)
        IF v_first AND p_start_id IS NULL THEN
            -- genesis row: prev_hash MUST be NULL
            IF r.prev_hash IS NOT NULL THEN
                bad_id         := r.id;
                bad_event_uuid := r.event_uuid;
                expected_hash  := NULL;
                stored_hash    := r.row_hash;
                expected_prev  := NULL;
                stored_prev    := r.prev_hash;
                RETURN NEXT;
                RETURN;
            END IF;
        ELSE
            IF r.prev_hash IS DISTINCT FROM v_prev_hash THEN
                bad_id         := r.id;
                bad_event_uuid := r.event_uuid;
                expected_hash  := NULL;
                stored_hash    := r.row_hash;
                expected_prev  := v_prev_hash;
                stored_prev    := r.prev_hash;
                RETURN NEXT;
                RETURN;
            END IF;
        END IF;

        v_canonical :=
            COALESCE(r.prev_hash,           '') || E'\n' ||
            r.event_uuid::text                   || E'\n' ||
            COALESCE(r.merchant_id::text,   '') || E'\n' ||
            r.actor_type                         || E'\n' ||
            COALESCE(r.actor_user_id::text, '') || E'\n' ||
            r.action                             || E'\n' ||
            COALESCE(r.resource_type,       '') || E'\n' ||
            COALESCE(r.resource_id,         '') || E'\n' ||
            r.payload::text                      || E'\n' ||
            COALESCE(host(r.ip_address),    '') || E'\n' ||
            COALESCE(r.request_id,          '') || E'\n' ||
            r.created_at::text;

        v_expected := encode(digest(v_canonical, 'sha256'), 'hex');

        IF v_expected IS DISTINCT FROM r.row_hash THEN
            bad_id         := r.id;
            bad_event_uuid := r.event_uuid;
            expected_hash  := v_expected;
            stored_hash    := r.row_hash;
            expected_prev  := v_prev_hash;
            stored_prev    := r.prev_hash;
            RETURN NEXT;
            RETURN;
        END IF;

        v_prev_hash := r.row_hash;
        v_first     := FALSE;
    END LOOP;

    -- All rows valid — return empty
    RETURN;
END;
$$;


-- ── 5. RBAC permissions ─────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
    ('audit.read'),       -- merchant: read own audit
    ('audit.read.all'),   -- admin: read across merchants
    ('audit.verify')      -- admin: verify chain
ON CONFLICT (key) DO NOTHING;

-- Owner: can read own audit events
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key = 'audit.read'
 WHERE r.name = 'owner'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Manager: can read own audit events
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key = 'audit.read'
 WHERE r.name = 'manager'
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
