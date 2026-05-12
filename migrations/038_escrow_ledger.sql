-- ════════════════════════════════════════════════════════════════════════════
-- 038_escrow_ledger.sql — Phase 2 of Bittu fintech reconciliation core
--
-- Escrow Ledger
-- ─────────────
-- A SECOND, INDEPENDENT, append-only ledger that tracks funds held in
-- escrow between payment-received and settlement-released to the merchant's
-- bank account.  Runs in parallel to merchant_ledger (Phase 1) and the
-- legacy journal_entries GL — does NOT replace them.
--
-- Why a separate ledger?
--   * Held balance is an entirely different liability than the merchant's
--     available balance — keeping them on one ledger forces every reader
--     to filter by status.  Separate tables = separate locks, no contention,
--     unambiguous audit.
--   * Each escrow_release entry points back to the originating escrow_hold,
--     so partial releases / clawbacks have a clean ancestry graph that
--     would be awkward to model on the unified ledger.
--   * Holds expire on a per-merchant T+N timer (cron-driven).  Querying
--     "what's due to release now" is a hot path; isolating it keeps
--     merchant_ledger's indexes lean.
--
-- Same invariants as Phase 1:
--   * Append-only via BEFORE UPDATE/DELETE trigger raising P0002
--   * Posting only via fn_post_escrow_ledger_entry()
--   * Idempotency + reference uniqueness via companion tables (partition-
--     key constraint workaround)
--   * Per-merchant balance lock row taken FOR UPDATE inside the post fn
-- ════════════════════════════════════════════════════════════════════════════

-- ────────────────────────────────────────────────────────────────────────────
-- 1. Enum: escrow transaction types
-- ────────────────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'escrow_txn_type') THEN
        CREATE TYPE escrow_txn_type AS ENUM (
            'escrow_hold',         -- CREDIT: funds enter escrow on payment
            'escrow_release',      -- DEBIT:  funds leave escrow on settlement
            'escrow_refund',       -- DEBIT:  refunded directly from escrow
            'escrow_chargeback',   -- DEBIT:  customer chargeback while held
            'escrow_expired',      -- DEBIT:  written off / aged out
            'escrow_adjustment'    -- DEBIT or CREDIT: manual admin correction
        );
    END IF;
END$$;


-- ────────────────────────────────────────────────────────────────────────────
-- 2. Per-merchant escrow config (T+N hold window)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_escrow_config (
    merchant_id   UUID         PRIMARY KEY,
    hold_days     INT          NOT NULL DEFAULT 1
                  CHECK (hold_days >= 0 AND hold_days <= 90),
    enabled       BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);


-- ────────────────────────────────────────────────────────────────────────────
-- 3. Per-(merchant, currency) balance lock for escrow
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escrow_balance_locks (
    merchant_id        UUID            NOT NULL,
    currency           CHAR(3)         NOT NULL DEFAULT 'INR',
    held_balance       NUMERIC(18,4)   NOT NULL DEFAULT 0,
    last_entry_id      UUID,
    last_posted_at     TIMESTAMPTZ,
    version            BIGINT          NOT NULL DEFAULT 0,
    PRIMARY KEY (merchant_id, currency)
);


-- ────────────────────────────────────────────────────────────────────────────
-- 4. Append-only escrow_ledger (partitioned by month on created_at)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escrow_ledger (
    id                 UUID            NOT NULL DEFAULT gen_random_uuid(),
    merchant_id        UUID            NOT NULL,
    branch_id          UUID,

    escrow_reference   TEXT            NOT NULL,    -- ESC-YYYYMM-NNNNNNNN
    transaction_type   escrow_txn_type NOT NULL,

    debit_amount       NUMERIC(18,4)   NOT NULL DEFAULT 0,
    credit_amount      NUMERIC(18,4)   NOT NULL DEFAULT 0,
    balance_after      NUMERIC(18,4)   NOT NULL,    -- held balance after this entry
    currency           CHAR(3)         NOT NULL DEFAULT 'INR',

    -- Source linkage
    source_type        TEXT,
    source_id          UUID,
    payment_id         UUID,
    settlement_id      UUID,
    order_id           UUID,
    bank_reference     TEXT,

    -- Hold lifecycle
    --   * On escrow_hold:    hold_until set; released_entry_id NULL.
    --   * On escrow_release: released_entry_id points to the originating
    --                        escrow_hold row (same merchant).
    hold_until         TIMESTAMPTZ,
    released_entry_id  UUID,

    idempotency_key    TEXT,
    metadata           JSONB           NOT NULL DEFAULT '{}'::jsonb,

    created_at         TIMESTAMPTZ     NOT NULL DEFAULT now(),
    created_by         UUID,

    CONSTRAINT pk_escrow_ledger PRIMARY KEY (id, created_at),

    CONSTRAINT chk_escrow_ledger_amounts CHECK (
        debit_amount  >= 0
        AND credit_amount >= 0
        AND (debit_amount > 0) <> (credit_amount > 0)
    ),
    CONSTRAINT chk_escrow_ledger_currency CHECK (currency = upper(currency))
) PARTITION BY RANGE (created_at);

-- Default partition
CREATE TABLE IF NOT EXISTS escrow_ledger_default
    PARTITION OF escrow_ledger DEFAULT;

-- Seed monthly partitions: current month + next 12.
DO $seed$
DECLARE
    start_month DATE := date_trunc('month', now())::date;
    i INT;
    pstart DATE;
    pend   DATE;
    pname  TEXT;
BEGIN
    FOR i IN 0..12 LOOP
        pstart := (start_month + (i || ' months')::interval)::date;
        pend   := (start_month + ((i + 1) || ' months')::interval)::date;
        pname  := format('escrow_ledger_y%sm%s',
                         to_char(pstart, 'YYYY'), to_char(pstart, 'MM'));
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF escrow_ledger
             FOR VALUES FROM (%L) TO (%L)',
            pname, pstart, pend
        );
    END LOOP;
END
$seed$;


-- ────────────────────────────────────────────────────────────────────────────
-- 5. Indexes (on partitioned parent → propagated to all partitions)
-- ────────────────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_escrow_ledger_merchant_created
    ON escrow_ledger (merchant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_branch_created
    ON escrow_ledger (branch_id, created_at DESC)
    WHERE branch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_txn_type
    ON escrow_ledger (merchant_id, transaction_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_payment
    ON escrow_ledger (payment_id) WHERE payment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_settlement
    ON escrow_ledger (settlement_id) WHERE settlement_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_released_entry
    ON escrow_ledger (released_entry_id) WHERE released_entry_id IS NOT NULL;

-- HOT PATH: cron job that finds holds eligible for release.
-- Filters on (txn_type='escrow_hold', released_entry_id IS NULL, hold_until <= now).
CREATE INDEX IF NOT EXISTS idx_escrow_ledger_due_release
    ON escrow_ledger (hold_until)
    WHERE transaction_type = 'escrow_hold' AND hold_until IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_idem_lookup
    ON escrow_ledger (merchant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_escrow_ledger_reference_lookup
    ON escrow_ledger (merchant_id, escrow_reference);


-- Companion uniqueness tables (NOT partitioned; PK enforces uniqueness
-- the partitioned parent cannot).
CREATE TABLE IF NOT EXISTS escrow_ledger_idempotency (
    merchant_id       UUID        NOT NULL,
    idempotency_key   TEXT        NOT NULL,
    ledger_id         UUID        NOT NULL,
    ledger_created_at TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (merchant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS escrow_ledger_references (
    merchant_id       UUID        NOT NULL,
    escrow_reference  TEXT        NOT NULL,
    ledger_id         UUID        NOT NULL,
    ledger_created_at TIMESTAMPTZ NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (merchant_id, escrow_reference)
);

-- Tracks "this hold has been (fully) released by THIS release entry".
-- Enforces that one escrow_hold can be released at most ONCE.  Multiple
-- partial releases against a single hold are not supported in Phase 2;
-- if needed later, drop this PK and switch to a sum check.
CREATE TABLE IF NOT EXISTS escrow_release_links (
    merchant_id       UUID        NOT NULL,
    hold_entry_id     UUID        NOT NULL,
    release_entry_id  UUID        NOT NULL,
    released_amount   NUMERIC(18,4) NOT NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (merchant_id, hold_entry_id)
);


-- ────────────────────────────────────────────────────────────────────────────
-- 6. Reference-number sequence (per-merchant monthly counter)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS escrow_ledger_seq (
    merchant_id  UUID    NOT NULL,
    yyyymm       CHAR(6) NOT NULL,
    last_seq     BIGINT  NOT NULL DEFAULT 0,
    PRIMARY KEY (merchant_id, yyyymm)
);


-- ────────────────────────────────────────────────────────────────────────────
-- 7. Immutability triggers
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_immutable_escrow_ledger()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'escrow_ledger is append-only — cannot % rows. Post a reversing entry instead.',
        TG_OP
        USING ERRCODE = 'P0002';
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_immutable_escrow_ledger ON escrow_ledger;
CREATE TRIGGER trg_immutable_escrow_ledger
    BEFORE UPDATE OR DELETE ON escrow_ledger
    FOR EACH ROW EXECUTE FUNCTION fn_immutable_escrow_ledger();


-- ────────────────────────────────────────────────────────────────────────────
-- 8. Posting function — THE ONLY supported escrow write path.
--
--    Atomic, idempotent, race-safe.  For escrow_release entries, callers
--    MUST pass p_released_entry_id pointing to the originating hold; the
--    function enforces single-release via escrow_release_links PK.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_post_escrow_ledger_entry(
    p_merchant_id        UUID,
    p_branch_id          UUID,
    p_transaction_type   escrow_txn_type,
    p_debit_amount       NUMERIC,
    p_credit_amount      NUMERIC,
    p_currency           CHAR(3) DEFAULT 'INR',
    p_source_type        TEXT    DEFAULT NULL,
    p_source_id          UUID    DEFAULT NULL,
    p_payment_id         UUID    DEFAULT NULL,
    p_settlement_id      UUID    DEFAULT NULL,
    p_order_id           UUID    DEFAULT NULL,
    p_bank_reference     TEXT    DEFAULT NULL,
    p_hold_until         TIMESTAMPTZ DEFAULT NULL,
    p_released_entry_id  UUID    DEFAULT NULL,
    p_idempotency_key    TEXT    DEFAULT NULL,
    p_metadata           JSONB   DEFAULT '{}'::jsonb,
    p_created_by         UUID    DEFAULT NULL
) RETURNS JSONB AS $$
DECLARE
    v_currency   CHAR(3) := upper(p_currency);
    v_debit      NUMERIC(18,4) := COALESCE(p_debit_amount, 0);
    v_credit     NUMERIC(18,4) := COALESCE(p_credit_amount, 0);
    v_existing   escrow_ledger%ROWTYPE;
    v_prev_bal   NUMERIC(18,4);
    v_new_bal    NUMERIC(18,4);
    v_yyyymm     CHAR(6) := to_char(now() AT TIME ZONE 'UTC', 'YYYYMM');
    v_seq        BIGINT;
    v_ref        TEXT;
    v_id         UUID := gen_random_uuid();
    v_now        TIMESTAMPTZ := now();
    v_row        escrow_ledger%ROWTYPE;
    v_hold       escrow_ledger%ROWTYPE;
BEGIN
    -- ── Validation ──────────────────────────────────────────────────────
    IF p_merchant_id IS NULL THEN
        RAISE EXCEPTION 'merchant_id is required';
    END IF;
    IF v_debit < 0 OR v_credit < 0 THEN
        RAISE EXCEPTION 'debit/credit must be non-negative';
    END IF;
    IF (v_debit > 0) = (v_credit > 0) THEN
        RAISE EXCEPTION 'exactly one of debit_amount / credit_amount must be > 0';
    END IF;

    -- escrow_hold must be a credit and SHOULD have hold_until
    IF p_transaction_type = 'escrow_hold' AND v_credit <= 0 THEN
        RAISE EXCEPTION 'escrow_hold entries must use credit_amount';
    END IF;

    -- escrow_release must be a debit and MUST link to a hold
    IF p_transaction_type = 'escrow_release' THEN
        IF v_debit <= 0 THEN
            RAISE EXCEPTION 'escrow_release entries must use debit_amount';
        END IF;
        IF p_released_entry_id IS NULL THEN
            RAISE EXCEPTION 'escrow_release requires p_released_entry_id linking to the originating hold';
        END IF;
    END IF;

    -- ── Idempotency short-circuit ────────────────────────────────────────
    IF p_idempotency_key IS NOT NULL THEN
        DECLARE
            v_existing_id UUID;
            v_existing_ts TIMESTAMPTZ;
        BEGIN
            SELECT ledger_id, ledger_created_at
              INTO v_existing_id, v_existing_ts
              FROM escrow_ledger_idempotency
             WHERE merchant_id     = p_merchant_id
               AND idempotency_key = p_idempotency_key;
            IF v_existing_id IS NOT NULL THEN
                SELECT * INTO v_existing
                  FROM escrow_ledger
                 WHERE id = v_existing_id AND created_at = v_existing_ts;
                IF FOUND THEN
                    RETURN to_jsonb(v_existing);
                END IF;
            END IF;
        END;
    END IF;

    -- ── Lock per-(merchant,currency) escrow balance ─────────────────────
    INSERT INTO escrow_balance_locks (merchant_id, currency)
    VALUES (p_merchant_id, v_currency)
    ON CONFLICT (merchant_id, currency) DO NOTHING;

    SELECT held_balance INTO v_prev_bal
    FROM escrow_balance_locks
    WHERE merchant_id = p_merchant_id AND currency = v_currency
    FOR UPDATE;

    v_new_bal := v_prev_bal + v_credit - v_debit;

    IF v_new_bal < 0 THEN
        RAISE EXCEPTION 'escrow balance would go negative (% + % - %); refusing to post',
            v_prev_bal, v_credit, v_debit
            USING ERRCODE = 'P0001';
    END IF;

    -- ── Validate release linkage BEFORE insert ──────────────────────────
    IF p_transaction_type = 'escrow_release' THEN
        SELECT * INTO v_hold
          FROM escrow_ledger
         WHERE id = p_released_entry_id
           AND merchant_id = p_merchant_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'released_entry_id % not found for merchant %',
                p_released_entry_id, p_merchant_id;
        END IF;
        IF v_hold.transaction_type <> 'escrow_hold' THEN
            RAISE EXCEPTION 'released_entry_id % is a %, not an escrow_hold',
                p_released_entry_id, v_hold.transaction_type;
        END IF;
        -- Single-release contract enforced via escrow_release_links PK below.
    END IF;

    -- ── Allocate per-merchant monthly sequence → escrow_reference ───────
    INSERT INTO escrow_ledger_seq (merchant_id, yyyymm, last_seq)
    VALUES (p_merchant_id, v_yyyymm, 1)
    ON CONFLICT (merchant_id, yyyymm) DO UPDATE
        SET last_seq = escrow_ledger_seq.last_seq + 1
    RETURNING last_seq INTO v_seq;

    v_ref := format('ESC-%s-%s', v_yyyymm, lpad(v_seq::text, 8, '0'));

    -- ── Insert immutable row ─────────────────────────────────────────────
    INSERT INTO escrow_ledger (
        id, merchant_id, branch_id, escrow_reference, transaction_type,
        debit_amount, credit_amount, balance_after, currency,
        source_type, source_id, payment_id, settlement_id, order_id,
        bank_reference, hold_until, released_entry_id,
        idempotency_key, metadata, created_at, created_by
    ) VALUES (
        v_id, p_merchant_id, p_branch_id, v_ref, p_transaction_type,
        v_debit, v_credit, v_new_bal, v_currency,
        p_source_type, p_source_id, p_payment_id, p_settlement_id, p_order_id,
        p_bank_reference, p_hold_until, p_released_entry_id,
        p_idempotency_key, COALESCE(p_metadata, '{}'::jsonb),
        v_now, p_created_by
    ) RETURNING * INTO v_row;

    -- ── Companion uniqueness writes ──────────────────────────────────────
    INSERT INTO escrow_ledger_references
        (merchant_id, escrow_reference, ledger_id, ledger_created_at)
    VALUES (p_merchant_id, v_ref, v_id, v_now);

    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO escrow_ledger_idempotency
            (merchant_id, idempotency_key, ledger_id, ledger_created_at)
        VALUES (p_merchant_id, p_idempotency_key, v_id, v_now);
    END IF;

    -- ── Single-release link (PK enforces no double-release) ─────────────
    IF p_transaction_type = 'escrow_release' THEN
        INSERT INTO escrow_release_links
            (merchant_id, hold_entry_id, release_entry_id, released_amount)
        VALUES (p_merchant_id, p_released_entry_id, v_id, v_debit);
    END IF;

    -- ── Update lock row ──────────────────────────────────────────────────
    UPDATE escrow_balance_locks
       SET held_balance   = v_new_bal,
           last_entry_id  = v_id,
           last_posted_at = v_now,
           version        = version + 1
     WHERE merchant_id = p_merchant_id AND currency = v_currency;

    RETURN to_jsonb(v_row);
END;
$$ LANGUAGE plpgsql;


-- ────────────────────────────────────────────────────────────────────────────
-- 9. Consistency check
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_check_escrow_consistency(
    p_merchant_id UUID,
    p_currency    CHAR(3) DEFAULT 'INR'
) RETURNS JSONB AS $$
DECLARE
    v_currency      CHAR(3) := upper(p_currency);
    v_sum_balance   NUMERIC(18,4);
    v_lock_balance  NUMERIC(18,4);
    v_last_after    NUMERIC(18,4);
    v_entry_count   BIGINT;
    v_open_holds    NUMERIC(18,4);
BEGIN
    SELECT COALESCE(SUM(credit_amount - debit_amount), 0), COUNT(*)
      INTO v_sum_balance, v_entry_count
      FROM escrow_ledger
     WHERE merchant_id = p_merchant_id AND currency = v_currency;

    SELECT held_balance INTO v_lock_balance
      FROM escrow_balance_locks
     WHERE merchant_id = p_merchant_id AND currency = v_currency;

    SELECT balance_after INTO v_last_after
      FROM escrow_ledger
     WHERE merchant_id = p_merchant_id AND currency = v_currency
     ORDER BY created_at DESC, escrow_reference DESC
     LIMIT 1;

    -- Sum of unreleased holds (sanity: should equal sum_of_movements when
    -- all activity is hold/release; refunds/chargebacks make these diverge).
    SELECT COALESCE(SUM(h.credit_amount), 0)
      INTO v_open_holds
      FROM escrow_ledger h
      LEFT JOIN escrow_release_links rl
             ON rl.merchant_id = h.merchant_id
            AND rl.hold_entry_id = h.id
     WHERE h.merchant_id = p_merchant_id
       AND h.currency    = v_currency
       AND h.transaction_type = 'escrow_hold'
       AND rl.hold_entry_id IS NULL;

    RETURN jsonb_build_object(
        'merchant_id',          p_merchant_id,
        'currency',             v_currency,
        'entry_count',          v_entry_count,
        'sum_of_movements',     v_sum_balance,
        'lock_balance',         COALESCE(v_lock_balance, 0),
        'last_balance_after',   COALESCE(v_last_after, 0),
        'open_holds_total',     v_open_holds,
        'lock_matches_sum',     COALESCE(v_lock_balance, 0) = v_sum_balance,
        'last_after_matches_sum', COALESCE(v_last_after, 0) = v_sum_balance,
        'checked_at',           now()
    );
END;
$$ LANGUAGE plpgsql STABLE;


-- ────────────────────────────────────────────────────────────────────────────
-- 10. Due-for-release helper — used by the cron job.
--
--     Returns escrow_hold rows whose hold_until has elapsed and which have
--     NOT yet been released (no row in escrow_release_links).
--
--     Caller iterates and posts an escrow_release for each.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_select_due_escrow_holds(
    p_now    TIMESTAMPTZ DEFAULT now(),
    p_limit  INT         DEFAULT 100
) RETURNS TABLE (
    hold_id        UUID,
    merchant_id    UUID,
    branch_id      UUID,
    currency       CHAR(3),
    credit_amount  NUMERIC,
    payment_id     UUID,
    order_id       UUID,
    hold_until     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ,
    metadata       JSONB
) AS $$
    SELECT h.id,
           h.merchant_id,
           h.branch_id,
           h.currency,
           h.credit_amount,
           h.payment_id,
           h.order_id,
           h.hold_until,
           h.created_at,
           h.metadata
      FROM escrow_ledger h
      LEFT JOIN escrow_release_links rl
             ON rl.merchant_id = h.merchant_id
            AND rl.hold_entry_id = h.id
     WHERE h.transaction_type = 'escrow_hold'
       AND h.hold_until IS NOT NULL
       AND h.hold_until <= p_now
       AND rl.hold_entry_id IS NULL
     ORDER BY h.hold_until ASC, h.created_at ASC
     LIMIT p_limit;
$$ LANGUAGE sql STABLE;


-- ────────────────────────────────────────────────────────────────────────────
-- 11. RBAC permissions
-- ────────────────────────────────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
    ('escrow.read'),
    ('escrow.write'),
    ('escrow.admin')
ON CONFLICT (key) DO NOTHING;

-- Owner: all 3
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('escrow.read', 'escrow.write', 'escrow.admin')
 WHERE r.name = 'owner'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Manager: read + write
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('escrow.read', 'escrow.write')
 WHERE r.name = 'manager'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Cashier / Waiter / Staff: read only
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key = 'escrow.read'
 WHERE r.name IN ('cashier', 'waiter', 'staff')
ON CONFLICT (role_id, permission_id) DO NOTHING;
