-- ============================================================================
-- 052_merchant_liability_ledger.sql
-- ----------------------------------------------------------------------------
-- Bank-grade merchant payable accounting.
--
-- WHY a separate ledger?
-- ----------------------
-- `merchant_ledger` (037) records merchant-facing balance movements
-- (payments received, refunds, fees etc.).
-- `escrow_ledger` (038) records the bank/nodal-side custody of those funds.
--
-- Neither captures, in a single place, the *liability* side of the platform's
-- books — i.e. "how much do we owe each merchant right now and what is its
-- aging?". Banks and auditors look at this number first. It is also what
-- backs settlement obligations, reserve holds, and refund liabilities.
--
-- This ledger is ALSO append-only. Adjustments are reversal entries.
-- It is partitioned monthly by created_at to stay performant at scale.
-- ============================================================================

BEGIN;

DO $$ BEGIN
    CREATE TYPE merchant_liability_kind AS ENUM (
        'settlement_obligation',     -- net payable accumulated for next settlement cycle
        'reserve_hold',              -- platform-held risk reserve
        'reserve_release',           -- reserve released back to merchant
        'refund_liability',          -- refund booked, not yet disbursed
        'dispute_provision',         -- disputed amount provisioned
        'dispute_release',           -- dispute won → release provision
        'payout_initiated',          -- payout sent to bank
        'payout_failed',             -- payout failed → reversion
        'manual_adjustment',         -- ops adjustment (always paired w/ reversal)
        'reversal'                   -- explicit reversal of a prior entry
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS merchant_liability_ledger (
    id                 UUID         NOT NULL DEFAULT gen_random_uuid(),
    merchant_id        UUID         NOT NULL,
    branch_id          UUID,
    liability_kind     merchant_liability_kind NOT NULL,

    -- "credit_amount increases what we owe the merchant; debit_amount
    --  reduces it." Mirrors the merchant_ledger sign convention.
    debit_amount       NUMERIC(18,4) NOT NULL DEFAULT 0,
    credit_amount      NUMERIC(18,4) NOT NULL DEFAULT 0,
    balance_after      NUMERIC(18,4) NOT NULL,
    currency           CHAR(3)       NOT NULL DEFAULT 'INR',

    -- Source linkage
    source_type        TEXT,                    -- 'payment'|'refund'|'settlement'|'dispute'|'payout'|'manual'
    source_id          UUID,
    payment_id         UUID,
    refund_id          UUID,
    settlement_id      UUID,
    payout_id          UUID,
    dispute_id         UUID,

    -- Aging support: when does this liability "fall due" / become payable?
    due_at             TIMESTAMPTZ,
    aged_bucket        TEXT,                    -- denormalised at snapshot time

    -- Reversal lineage (append-only)
    reversed_entry_id  UUID,                    -- present on reversal entries
    reversal_reason    TEXT,

    idempotency_key    TEXT,
    metadata           JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_by         UUID,

    PRIMARY KEY (id, created_at),
    CONSTRAINT chk_mll_amounts CHECK (
        debit_amount  >= 0 AND credit_amount >= 0
        AND (debit_amount > 0) <> (credit_amount > 0)
    ),
    CONSTRAINT chk_mll_currency CHECK (currency = upper(currency))
) PARTITION BY RANGE (created_at);

CREATE TABLE IF NOT EXISTS merchant_liability_ledger_default
    PARTITION OF merchant_liability_ledger DEFAULT;

CREATE INDEX IF NOT EXISTS ix_mll_merchant_created
    ON merchant_liability_ledger (merchant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_mll_kind_created
    ON merchant_liability_ledger (merchant_id, liability_kind, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_mll_due_open
    ON merchant_liability_ledger (due_at)
    WHERE due_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_mll_settlement
    ON merchant_liability_ledger (settlement_id) WHERE settlement_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_mll_payout
    ON merchant_liability_ledger (payout_id) WHERE payout_id IS NOT NULL;

-- Companion idempotency table (PK can't span the partition key).
CREATE TABLE IF NOT EXISTS merchant_liability_idempotency (
    merchant_id      UUID        NOT NULL,
    idempotency_key  TEXT        NOT NULL,
    entry_id         UUID        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (merchant_id, idempotency_key)
);

-- Append-only enforcement (mirrors merchant_ledger pattern) -----------------
CREATE OR REPLACE FUNCTION fn_mll_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'merchant_liability_ledger is append-only (DELETE denied)';
    END IF;
    RAISE EXCEPTION 'merchant_liability_ledger is append-only (UPDATE denied)';
END $$;

DROP TRIGGER IF EXISTS trg_mll_no_update ON merchant_liability_ledger;
DROP TRIGGER IF EXISTS trg_mll_no_delete ON merchant_liability_ledger;
CREATE TRIGGER trg_mll_no_update BEFORE UPDATE ON merchant_liability_ledger
FOR EACH ROW EXECUTE FUNCTION fn_mll_block_mutation();
CREATE TRIGGER trg_mll_no_delete BEFORE DELETE ON merchant_liability_ledger
FOR EACH ROW EXECUTE FUNCTION fn_mll_block_mutation();


-- Single posting fn (idempotent, lock-serialised on merchant balance row) ---
-- Reuses the existing merchant_ledger_balance_locks table from migration 037
-- so the two ledgers stay locked on the SAME row (prevents balance drift).
CREATE OR REPLACE FUNCTION fn_post_merchant_liability_entry(
    p_merchant_id      UUID,
    p_branch_id        UUID,
    p_liability_kind   merchant_liability_kind,
    p_debit            NUMERIC,
    p_credit           NUMERIC,
    p_currency         CHAR(3),
    p_source_type      TEXT,
    p_source_id        UUID,
    p_metadata         JSONB,
    p_idempotency_key  TEXT,
    p_payment_id       UUID    DEFAULT NULL,
    p_refund_id        UUID    DEFAULT NULL,
    p_settlement_id    UUID    DEFAULT NULL,
    p_payout_id        UUID    DEFAULT NULL,
    p_dispute_id       UUID    DEFAULT NULL,
    p_due_at           TIMESTAMPTZ DEFAULT NULL,
    p_reversed_entry_id UUID   DEFAULT NULL,
    p_reversal_reason   TEXT   DEFAULT NULL,
    p_created_by        UUID   DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_existing_id UUID;
    v_balance     NUMERIC(18,4);
    v_new_id      UUID := gen_random_uuid();
    v_now         TIMESTAMPTZ := NOW();
BEGIN
    -- Idempotency short-circuit
    IF p_idempotency_key IS NOT NULL THEN
        SELECT entry_id INTO v_existing_id
          FROM merchant_liability_idempotency
         WHERE merchant_id = p_merchant_id
           AND idempotency_key = p_idempotency_key;
        IF v_existing_id IS NOT NULL THEN
            RETURN jsonb_build_object('entry_id', v_existing_id, 'idempotent', true);
        END IF;
    END IF;

    -- Lock the merchant balance row (reuse merchant_ledger_balance_locks).
    -- This serialises ALL liability + ledger writes per merchant.
    PERFORM 1 FROM merchant_ledger_balance_locks
        WHERE merchant_id = p_merchant_id
        FOR UPDATE;
    IF NOT FOUND THEN
        INSERT INTO merchant_ledger_balance_locks(merchant_id) VALUES (p_merchant_id);
    END IF;

    -- Compute new balance.
    SELECT COALESCE(balance_after, 0) INTO v_balance
      FROM merchant_liability_ledger
     WHERE merchant_id = p_merchant_id
     ORDER BY created_at DESC
     LIMIT 1;

    v_balance := COALESCE(v_balance, 0) + COALESCE(p_credit, 0) - COALESCE(p_debit, 0);

    INSERT INTO merchant_liability_ledger(
        id, merchant_id, branch_id, liability_kind,
        debit_amount, credit_amount, balance_after, currency,
        source_type, source_id, payment_id, refund_id,
        settlement_id, payout_id, dispute_id,
        due_at, reversed_entry_id, reversal_reason,
        idempotency_key, metadata, created_at, created_by)
    VALUES (
        v_new_id, p_merchant_id, p_branch_id, p_liability_kind,
        COALESCE(p_debit, 0), COALESCE(p_credit, 0), v_balance, COALESCE(p_currency, 'INR'),
        p_source_type, p_source_id, p_payment_id, p_refund_id,
        p_settlement_id, p_payout_id, p_dispute_id,
        p_due_at, p_reversed_entry_id, p_reversal_reason,
        p_idempotency_key, COALESCE(p_metadata, '{}'::jsonb), v_now, p_created_by
    );

    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO merchant_liability_idempotency(merchant_id, idempotency_key, entry_id)
        VALUES (p_merchant_id, p_idempotency_key, v_new_id);
    END IF;

    RETURN jsonb_build_object(
        'entry_id',     v_new_id,
        'balance_after', v_balance,
        'idempotent',   false
    );
END $$;

COMMIT;
