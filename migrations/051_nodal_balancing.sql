-- ============================================================================
-- 051_nodal_balancing.sql
-- ----------------------------------------------------------------------------
-- Adds the NODAL ACCOUNT layer and EOD BALANCING SNAPSHOTS on top of the
-- existing `escrow_ledger` (migration 038). We do not rebuild the ledger; we
-- give it the bank-facing context every fintech reconciliation engine needs.
--
-- Concepts
-- --------
--   nodal_accounts        — 1 row per real bank/nodal account we hold money in
--                           (RBI nodal a/c, escrow account at partner bank,
--                           internal float account). Acts as the "physical"
--                           container against which `escrow_ledger` (the
--                           "logical" ledger) is reconciled.
--
--   escrow_balance_snapshots
--                         — one row per (nodal_account_id, snapshot_date) with:
--                           * opening_balance  (carried from previous EOD)
--                           * credits          (sum of escrow_ledger credits in window)
--                           * debits           (sum of escrow_ledger debits in window)
--                           * computed_closing = opening + credits - debits
--                           * actual_bank_balance (filled by bank-statement importer)
--                           * variance         = computed - actual
--                           * status           (pending|matched|breach|investigating)
--                         The invariant is enforced at the function layer
--                         (fn_record_escrow_snapshot).
--
--   fn_escrow_eod_balance — given a nodal account + date, sums the day's
--                           escrow_ledger movements and returns JSONB so a
--                           worker can persist a snapshot atomically.
--
-- WHY this matters
-- ----------------
--   * Banks send T+1 settlement files. Without daily snapshots we cannot
--     prove "what we said we owed merchants" matched "what hit the bank".
--   * Every variance > 1 paisa is an audit incident. This table is the
--     source of evidence.
--   * Append-only: once a snapshot is matched it cannot be silently
--     "fixed up" by ops — only superseded by a new dated snapshot.
-- ============================================================================

BEGIN;

-- 1) nodal_accounts ----------------------------------------------------------
DO $$ BEGIN
    CREATE TYPE nodal_account_kind AS ENUM
        ('escrow', 'nodal', 'settlement', 'reserve', 'float', 'fee_collection');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE nodal_account_status AS ENUM
        ('active', 'frozen', 'closed');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS nodal_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                TEXT NOT NULL UNIQUE,            -- e.g. ICICI_NODAL_01
    label               TEXT NOT NULL,
    kind                nodal_account_kind NOT NULL,
    status              nodal_account_status NOT NULL DEFAULT 'active',

    bank_name           TEXT,
    bank_ifsc           TEXT,
    account_number_last4 CHAR(4),                        -- never store full number
    account_number_hash  TEXT,                           -- sha256 for matching

    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_nodal_accounts_kind ON nodal_accounts (kind, status);

-- Add nodal_account_id to escrow_ledger so each entry knows WHICH bank
-- container it lived in. Nullable for backward compat with existing rows.
ALTER TABLE escrow_ledger
    ADD COLUMN IF NOT EXISTS nodal_account_id UUID REFERENCES nodal_accounts(id);

CREATE INDEX IF NOT EXISTS ix_escrow_ledger_nodal_created
    ON escrow_ledger (nodal_account_id, created_at DESC)
    WHERE nodal_account_id IS NOT NULL;


-- 2) escrow_balance_snapshots -------------------------------------------------
DO $$ BEGIN
    CREATE TYPE escrow_snapshot_status AS ENUM
        ('pending', 'matched', 'breach', 'investigating', 'resolved');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS escrow_balance_snapshots (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nodal_account_id     UUID NOT NULL REFERENCES nodal_accounts(id),
    snapshot_date        DATE NOT NULL,                          -- the day this snapshot covers
    snapshot_window_start TIMESTAMPTZ NOT NULL,
    snapshot_window_end   TIMESTAMPTZ NOT NULL,

    opening_balance      NUMERIC(18,4) NOT NULL DEFAULT 0,
    credits              NUMERIC(18,4) NOT NULL DEFAULT 0,
    debits               NUMERIC(18,4) NOT NULL DEFAULT 0,
    computed_closing     NUMERIC(18,4) NOT NULL,
    actual_bank_balance  NUMERIC(18,4),                          -- filled later by importer
    variance             NUMERIC(18,4),                          -- computed - actual
    variance_paisa       BIGINT,                                 -- abs(variance) * 100, integer
    status               escrow_snapshot_status NOT NULL DEFAULT 'pending',
    breach_reason        TEXT,
    metadata             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    matched_at           TIMESTAMPTZ,
    UNIQUE (nodal_account_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS ix_escrow_snap_status_date
    ON escrow_balance_snapshots (status, snapshot_date DESC);


-- 3) fn_escrow_eod_balance ---------------------------------------------------
CREATE OR REPLACE FUNCTION fn_escrow_eod_balance(
    p_nodal_account_id UUID,
    p_snapshot_date    DATE
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_window_start TIMESTAMPTZ := (p_snapshot_date::timestamp AT TIME ZONE 'UTC');
    v_window_end   TIMESTAMPTZ := ((p_snapshot_date + 1)::timestamp AT TIME ZONE 'UTC');
    v_opening      NUMERIC(18,4) := 0;
    v_credits      NUMERIC(18,4) := 0;
    v_debits       NUMERIC(18,4) := 0;
    v_closing      NUMERIC(18,4);
BEGIN
    -- Opening = previous snapshot's computed_closing if present,
    -- else cumulative balance up to window start.
    SELECT computed_closing INTO v_opening
      FROM escrow_balance_snapshots
     WHERE nodal_account_id = p_nodal_account_id
       AND snapshot_date    = p_snapshot_date - 1
     LIMIT 1;
    IF v_opening IS NULL THEN
        SELECT COALESCE(SUM(credit_amount - debit_amount), 0)
          INTO v_opening
          FROM escrow_ledger
         WHERE nodal_account_id = p_nodal_account_id
           AND created_at < v_window_start;
    END IF;

    SELECT COALESCE(SUM(credit_amount), 0), COALESCE(SUM(debit_amount), 0)
      INTO v_credits, v_debits
      FROM escrow_ledger
     WHERE nodal_account_id = p_nodal_account_id
       AND created_at >= v_window_start
       AND created_at <  v_window_end;

    v_closing := v_opening + v_credits - v_debits;

    RETURN jsonb_build_object(
        'nodal_account_id', p_nodal_account_id,
        'snapshot_date',    p_snapshot_date,
        'window_start',     v_window_start,
        'window_end',       v_window_end,
        'opening_balance',  v_opening,
        'credits',          v_credits,
        'debits',           v_debits,
        'computed_closing', v_closing
    );
END $$;


-- 4) fn_record_escrow_snapshot (atomic, idempotent) --------------------------
CREATE OR REPLACE FUNCTION fn_record_escrow_snapshot(
    p_nodal_account_id  UUID,
    p_snapshot_date     DATE,
    p_actual_bank_balance NUMERIC DEFAULT NULL
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_eod   JSONB;
    v_id    UUID;
    v_var   NUMERIC(18,4);
    v_status escrow_snapshot_status;
BEGIN
    v_eod := fn_escrow_eod_balance(p_nodal_account_id, p_snapshot_date);

    IF p_actual_bank_balance IS NOT NULL THEN
        v_var := (v_eod->>'computed_closing')::numeric - p_actual_bank_balance;
        IF abs(v_var) <= 0.01 THEN
            v_status := 'matched';
        ELSE
            v_status := 'breach';
        END IF;
    ELSE
        v_status := 'pending';
    END IF;

    INSERT INTO escrow_balance_snapshots
        (nodal_account_id, snapshot_date, snapshot_window_start, snapshot_window_end,
         opening_balance, credits, debits, computed_closing,
         actual_bank_balance, variance, variance_paisa, status, matched_at)
    VALUES
        (p_nodal_account_id, p_snapshot_date,
         (v_eod->>'window_start')::timestamptz,
         (v_eod->>'window_end')::timestamptz,
         (v_eod->>'opening_balance')::numeric,
         (v_eod->>'credits')::numeric,
         (v_eod->>'debits')::numeric,
         (v_eod->>'computed_closing')::numeric,
         p_actual_bank_balance,
         v_var,
         CASE WHEN v_var IS NULL THEN NULL ELSE (abs(v_var) * 100)::bigint END,
         v_status,
         CASE WHEN v_status = 'matched' THEN NOW() ELSE NULL END)
    ON CONFLICT (nodal_account_id, snapshot_date) DO UPDATE
        SET opening_balance     = EXCLUDED.opening_balance,
            credits             = EXCLUDED.credits,
            debits              = EXCLUDED.debits,
            computed_closing    = EXCLUDED.computed_closing,
            actual_bank_balance = COALESCE(EXCLUDED.actual_bank_balance,
                                           escrow_balance_snapshots.actual_bank_balance),
            variance            = EXCLUDED.variance,
            variance_paisa      = EXCLUDED.variance_paisa,
            status              = EXCLUDED.status,
            matched_at          = COALESCE(EXCLUDED.matched_at,
                                           escrow_balance_snapshots.matched_at)
    RETURNING id INTO v_id;

    RETURN v_id;
END $$;

-- Snapshot rows are append-only by convention; the only legal mutator is
-- fn_record_escrow_snapshot's ON CONFLICT DO UPDATE above. Ops cannot DELETE.
CREATE OR REPLACE FUNCTION fn_escrow_snapshot_block_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'escrow_balance_snapshots is append-only (DELETE denied)';
END $$;
DROP TRIGGER IF EXISTS trg_escrow_snap_no_delete ON escrow_balance_snapshots;
CREATE TRIGGER trg_escrow_snap_no_delete
BEFORE DELETE ON escrow_balance_snapshots
FOR EACH ROW EXECUTE FUNCTION fn_escrow_snapshot_block_delete();

COMMIT;
