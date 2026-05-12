-- ════════════════════════════════════════════════════════════════════════════
-- Phase 4 — Payouts / Disbursement Engine
--
-- Manages outgoing payouts to merchants:
--   • per-merchant beneficiary registry (bank account / UPI VPA)
--   • payout requests with strict status machine
--   • batches that group approved payouts for file generation
--   • status event trail (audit log)
--   • debits merchant_ledger via fn_post_merchant_ledger_entry on "sent"
--   • credits back via "payout_reversed" txn on "failed"
--
-- HARD RULE: This module DOES NOT call any payment gateway. It only
-- generates a CSV file the operator uploads to the bank portal manually.
--
-- Author: Phase 4 fintech recon
-- ════════════════════════════════════════════════════════════════════════════
BEGIN;

-- ── 1. Enums ────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payout_status') THEN
        CREATE TYPE payout_status AS ENUM (
            'draft',        -- merchant editing (not used yet, reserved)
            'requested',    -- merchant submitted, awaits admin approval
            'approved',     -- admin approved, eligible for batching
            'rejected',     -- admin rejected, terminal
            'cancelled',    -- merchant cancelled before approval, terminal
            'queued',       -- in a batch, awaiting file generation
            'processing',   -- file generated, operator uploading to bank
            'sent',         -- bank ack received, merchant_ledger debited
            'completed',    -- bank confirmed credit to beneficiary, terminal
            'failed'        -- bank rejected; ledger reversed if was 'sent'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payout_method') THEN
        CREATE TYPE payout_method AS ENUM ('bank_neft', 'bank_imps', 'bank_rtgs', 'upi');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payout_beneficiary_type') THEN
        CREATE TYPE payout_beneficiary_type AS ENUM ('bank', 'upi');
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payout_batch_status') THEN
        CREATE TYPE payout_batch_status AS ENUM (
            'open', 'file_generated', 'processing', 'closed', 'cancelled'
        );
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'payout_event_type') THEN
        CREATE TYPE payout_event_type AS ENUM (
            'created', 'approved', 'rejected', 'cancelled',
            'batched', 'unbatched', 'file_generated',
            'sent', 'completed', 'failed', 'reversed', 'note'
        );
    END IF;
END$$;


-- Extend merchant_ledger txn type with payout-specific kinds.
-- These are new values only; existing enum entries are preserved.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum
         WHERE enumtypid = 'merchant_ledger_txn_type'::regtype
           AND enumlabel = 'payout_initiated'
    ) THEN
        ALTER TYPE merchant_ledger_txn_type ADD VALUE 'payout_initiated';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum
         WHERE enumtypid = 'merchant_ledger_txn_type'::regtype
           AND enumlabel = 'payout_completed'
    ) THEN
        ALTER TYPE merchant_ledger_txn_type ADD VALUE 'payout_completed';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum
         WHERE enumtypid = 'merchant_ledger_txn_type'::regtype
           AND enumlabel = 'payout_reversed'
    ) THEN
        ALTER TYPE merchant_ledger_txn_type ADD VALUE 'payout_reversed';
    END IF;
END$$;


-- ── 2. Beneficiaries ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payout_beneficiaries (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id         UUID NOT NULL,
    label               TEXT NOT NULL,
    type                payout_beneficiary_type NOT NULL,

    -- Bank fields (required when type='bank')
    account_holder      TEXT,
    account_number      TEXT,
    account_number_last4 TEXT,
    ifsc                TEXT,
    bank_name           TEXT,

    -- UPI field (required when type='upi')
    upi_vpa             TEXT,

    -- Verification + lifecycle
    is_active           BOOLEAN NOT NULL DEFAULT true,
    is_verified         BOOLEAN NOT NULL DEFAULT false,
    verified_at         TIMESTAMPTZ,
    verified_by         UUID,

    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          UUID,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Per-merchant unique label
    UNIQUE (merchant_id, label),

    CONSTRAINT chk_payout_ben_kind CHECK (
        (type = 'bank' AND account_number IS NOT NULL AND ifsc IS NOT NULL)
        OR
        (type = 'upi' AND upi_vpa IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_payout_ben_merchant_active
    ON payout_beneficiaries (merchant_id, is_active);


-- ── 3. Batches (admin grouping for file generation) ─────────────────────
CREATE TABLE IF NOT EXISTS payout_batches (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_reference     TEXT NOT NULL UNIQUE,
    status              payout_batch_status NOT NULL DEFAULT 'open',

    -- Aggregates (maintained by service; recomputed on demand)
    total_amount        NUMERIC(18,4) NOT NULL DEFAULT 0,
    total_count         INTEGER NOT NULL DEFAULT 0,
    currency            CHAR(3) NOT NULL DEFAULT 'INR',

    file_format         TEXT,         -- e.g. 'neft_csv', 'imps_csv'
    file_generated_at   TIMESTAMPTZ,
    file_path           TEXT,         -- if persisted to disk; usually NULL (downloaded inline)

    notes               TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by          UUID,         -- admin user
    closed_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_payout_batches_status
    ON payout_batches (status, created_at DESC);


-- ── 4. Payout requests (the core table) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS payout_requests (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_reference    TEXT NOT NULL UNIQUE,

    merchant_id         UUID NOT NULL,
    branch_id           UUID,
    beneficiary_id      UUID NOT NULL REFERENCES payout_beneficiaries(id),

    amount              NUMERIC(18,4) NOT NULL CHECK (amount > 0),
    currency            CHAR(3) NOT NULL DEFAULT 'INR',
    method              payout_method NOT NULL,

    status              payout_status NOT NULL DEFAULT 'requested',

    -- Audit / actor fields
    requested_by        UUID NOT NULL,
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by         UUID,
    approved_at         TIMESTAMPTZ,
    rejected_by         UUID,
    rejected_at         TIMESTAMPTZ,
    rejection_reason    TEXT,
    cancelled_at        TIMESTAMPTZ,

    -- Batch + ledger linkage
    batch_id            UUID REFERENCES payout_batches(id),
    ledger_entry_id     UUID,    -- merchant_ledger entry id when 'sent'
    reversal_entry_id   UUID,    -- merchant_ledger entry id on 'failed' reversal

    -- Bank acknowledgement fields
    utr_number          TEXT,
    bank_reference      TEXT,
    sent_at             TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    failed_at           TIMESTAMPTZ,
    failure_reason      TEXT,

    notes               TEXT,
    idempotency_key     TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One idempotency row per merchant
    UNIQUE (merchant_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_payout_req_merchant_status
    ON payout_requests (merchant_id, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_payout_req_status
    ON payout_requests (status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_payout_req_batch
    ON payout_requests (batch_id);
CREATE INDEX IF NOT EXISTS idx_payout_req_ben
    ON payout_requests (beneficiary_id);


-- ── 5. Per-merchant monthly sequence (for payout_reference) ─────────────
CREATE TABLE IF NOT EXISTS payout_reference_seq (
    merchant_id UUID    NOT NULL,
    yyyymm      CHAR(6) NOT NULL,
    last_seq    BIGINT  NOT NULL DEFAULT 0,
    PRIMARY KEY (merchant_id, yyyymm)
);

-- Global batch sequence (admin-side, not per merchant)
CREATE TABLE IF NOT EXISTS payout_batch_seq (
    yyyymm   CHAR(6) PRIMARY KEY,
    last_seq BIGINT  NOT NULL DEFAULT 0
);


-- ── 6. Audit trail ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS payout_status_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payout_id       UUID NOT NULL REFERENCES payout_requests(id) ON DELETE CASCADE,
    event_type      payout_event_type NOT NULL,
    from_status     payout_status,
    to_status       payout_status,
    actor_user_id   UUID,
    is_admin_action BOOLEAN NOT NULL DEFAULT false,
    notes           TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payout_events_payout
    ON payout_status_events (payout_id, created_at DESC);


-- ── 7. Helper functions ─────────────────────────────────────────────────

-- Generate the next payout_reference for a merchant for the current month.
CREATE OR REPLACE FUNCTION fn_next_payout_reference(p_merchant_id UUID)
RETURNS TEXT AS $$
DECLARE
    v_yyyymm CHAR(6) := to_char(now() AT TIME ZONE 'UTC', 'YYYYMM');
    v_seq    BIGINT;
BEGIN
    INSERT INTO payout_reference_seq (merchant_id, yyyymm, last_seq)
    VALUES (p_merchant_id, v_yyyymm, 1)
    ON CONFLICT (merchant_id, yyyymm) DO UPDATE
       SET last_seq = payout_reference_seq.last_seq + 1
    RETURNING last_seq INTO v_seq;
    RETURN format('PO-%s-%s', v_yyyymm, lpad(v_seq::text, 8, '0'));
END;
$$ LANGUAGE plpgsql;

-- Generate the next batch_reference (global).
CREATE OR REPLACE FUNCTION fn_next_payout_batch_reference()
RETURNS TEXT AS $$
DECLARE
    v_yyyymm CHAR(6) := to_char(now() AT TIME ZONE 'UTC', 'YYYYMM');
    v_seq    BIGINT;
BEGIN
    INSERT INTO payout_batch_seq (yyyymm, last_seq)
    VALUES (v_yyyymm, 1)
    ON CONFLICT (yyyymm) DO UPDATE
       SET last_seq = payout_batch_seq.last_seq + 1
    RETURNING last_seq INTO v_seq;
    RETURN format('POBATCH-%s-%s', v_yyyymm, lpad(v_seq::text, 6, '0'));
END;
$$ LANGUAGE plpgsql;

-- Compute the merchant's *available* balance for new payout requests:
--   current_balance (from balance_locks)
--   minus the sum of pending payouts (requested + approved + queued + processing + sent
--                                     that have not been credited back)
-- "sent" payouts are already debited in merchant_ledger so they don't subtract again.
CREATE OR REPLACE FUNCTION fn_payout_available_balance(
    p_merchant_id UUID,
    p_currency    CHAR(3) DEFAULT 'INR'
) RETURNS NUMERIC AS $$
DECLARE
    v_balance NUMERIC(18,4);
    v_locked  NUMERIC(18,4);
BEGIN
    SELECT COALESCE(current_balance, 0) INTO v_balance
      FROM merchant_ledger_balance_locks
     WHERE merchant_id = p_merchant_id AND currency = upper(p_currency);

    -- Money already earmarked for in-flight payouts that haven't yet posted to ledger.
    SELECT COALESCE(SUM(amount), 0) INTO v_locked
      FROM payout_requests
     WHERE merchant_id = p_merchant_id
       AND currency = upper(p_currency)
       AND status IN ('requested', 'approved', 'queued', 'processing');

    RETURN COALESCE(v_balance, 0) - COALESCE(v_locked, 0);
END;
$$ LANGUAGE plpgsql STABLE;


-- ── 8. RBAC permissions ─────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
    ('payout.read'),
    ('payout.write'),
    ('payout.approve'),
    ('payout.admin')
ON CONFLICT (key) DO NOTHING;

-- Owner: read + write + approve (can approve own payouts in single-merchant setups
-- — admin endpoints additionally require platform_admin).
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('payout.read', 'payout.write', 'payout.approve')
 WHERE r.name = 'owner'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Manager: read + write (cannot approve)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key IN ('payout.read', 'payout.write')
 WHERE r.name = 'manager'
ON CONFLICT (role_id, permission_id) DO NOTHING;

-- Cashier/staff: read only
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
  FROM roles r
  JOIN permissions p ON p.key = 'payout.read'
 WHERE r.name IN ('cashier', 'waiter', 'staff')
ON CONFLICT (role_id, permission_id) DO NOTHING;


-- ── 9. updated_at trigger for payout_requests + beneficiaries ───────────
CREATE OR REPLACE FUNCTION fn_payout_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_payout_req_touch ON payout_requests;
CREATE TRIGGER trg_payout_req_touch
    BEFORE UPDATE ON payout_requests
    FOR EACH ROW EXECUTE FUNCTION fn_payout_touch_updated_at();

DROP TRIGGER IF EXISTS trg_payout_ben_touch ON payout_beneficiaries;
CREATE TRIGGER trg_payout_ben_touch
    BEFORE UPDATE ON payout_beneficiaries
    FOR EACH ROW EXECUTE FUNCTION fn_payout_touch_updated_at();


COMMIT;
