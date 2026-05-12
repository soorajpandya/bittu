-- =====================================================================
-- Migration 046 — Fee Engine v2 (Phase 10)
-- =====================================================================
-- Pluggable, per-merchant fee plans with rule precedence.
--
-- Design:
--   * fee_plans          — named plans, one default
--   * fee_plan_rules     — rules within a plan (method/source/amount-band)
--   * merchant_fee_overrides — schedule a non-default plan for a merchant
--   * fee_computations   — append-only audit of every computation
--
-- Resolution:
--   fn_resolve_fee_plan(merchant_id, at_ts) → effective plan_id
--   fn_compute_fee(merchant_id, gross, payment_method?, order_source?, currency?, at_ts?, record?, payment_id?)
--      returns JSONB { plan_id, rule_id, fee, gst, total_deduction, net, ... }
--
-- BACKWARD COMPAT: legacy `statement_service.TOTAL_DEDUCTION_RATE` math is
-- left intact; this engine is opt-in for new code paths.
-- =====================================================================

BEGIN;

-- ── 1. Enums ──────────────────────────────────────────────────────────
DO $$ BEGIN
  CREATE TYPE fee_calc_type AS ENUM ('percent', 'flat', 'percent_plus_flat');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 2. fee_plans ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fee_plans (
  id              BIGSERIAL PRIMARY KEY,
  plan_uuid       UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  code            TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  description     TEXT,
  currency        CHAR(3) NOT NULL DEFAULT 'INR',
  gst_rate        NUMERIC(8,6) NOT NULL DEFAULT 0.180000,
  is_active       BOOLEAN NOT NULL DEFAULT true,
  is_default      BOOLEAN NOT NULL DEFAULT false,
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to        TIMESTAMPTZ,
  metadata        JSONB NOT NULL DEFAULT '{}',
  created_by_admin_id UUID,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Only one default plan
CREATE UNIQUE INDEX IF NOT EXISTS uq_fee_plans_default
  ON fee_plans(is_default) WHERE is_default = true;
CREATE INDEX IF NOT EXISTS ix_fee_plans_active ON fee_plans(is_active);

-- ── 3. fee_plan_rules ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fee_plan_rules (
  id              BIGSERIAL PRIMARY KEY,
  rule_uuid       UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  plan_id         BIGINT NOT NULL REFERENCES fee_plans(id) ON DELETE CASCADE,
  payment_method  TEXT,                       -- NULL = any
  order_source    TEXT,                       -- NULL = any
  min_amount      NUMERIC(14,2) NOT NULL DEFAULT 0,
  max_amount      NUMERIC(14,2),              -- NULL = +∞
  fee_type        fee_calc_type NOT NULL DEFAULT 'percent',
  percent_rate    NUMERIC(8,6) NOT NULL DEFAULT 0,   -- e.g. 0.002542 = 0.2542%
  flat_fee        NUMERIC(12,2) NOT NULL DEFAULT 0,  -- in major units
  priority        INT NOT NULL DEFAULT 100,          -- higher = preferred
  is_active       BOOLEAN NOT NULL DEFAULT true,
  metadata        JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_fee_rule_amounts CHECK (
    min_amount >= 0 AND (max_amount IS NULL OR max_amount > min_amount)
  ),
  CONSTRAINT chk_fee_rule_rate CHECK (percent_rate >= 0 AND percent_rate <= 1),
  CONSTRAINT chk_fee_rule_flat CHECK (flat_fee >= 0)
);
CREATE INDEX IF NOT EXISTS ix_fee_rules_plan_active
  ON fee_plan_rules(plan_id, is_active, priority DESC);

-- ── 4. merchant_fee_overrides ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_fee_overrides (
  id              BIGSERIAL PRIMARY KEY,
  override_uuid   UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  merchant_id     UUID NOT NULL,
  plan_id         BIGINT NOT NULL REFERENCES fee_plans(id) ON DELETE RESTRICT,
  valid_from      TIMESTAMPTZ NOT NULL DEFAULT now(),
  valid_to        TIMESTAMPTZ,
  reason          TEXT,
  created_by_admin_id UUID,
  metadata        JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_override_window CHECK (
    valid_to IS NULL OR valid_to > valid_from
  )
);
CREATE INDEX IF NOT EXISTS ix_overrides_merchant
  ON merchant_fee_overrides(merchant_id, valid_from DESC);

-- ── 5. fee_computations (append-only audit) ───────────────────────────
CREATE TABLE IF NOT EXISTS fee_computations (
  id              BIGSERIAL PRIMARY KEY,
  computation_uuid UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
  merchant_id     UUID NOT NULL,
  payment_id      TEXT,
  plan_id         BIGINT NOT NULL,
  rule_id         BIGINT,
  payment_method  TEXT,
  order_source    TEXT,
  currency        CHAR(3) NOT NULL DEFAULT 'INR',
  gross_amount    NUMERIC(14,2) NOT NULL,
  fee_amount      NUMERIC(14,2) NOT NULL,
  gst_amount      NUMERIC(14,2) NOT NULL,
  total_deduction NUMERIC(14,2) NOT NULL,
  net_amount      NUMERIC(14,2) NOT NULL,
  breakdown       JSONB NOT NULL DEFAULT '{}',
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_fee_comp_merchant
  ON fee_computations(merchant_id, computed_at DESC);
CREATE INDEX IF NOT EXISTS ix_fee_comp_payment
  ON fee_computations(payment_id) WHERE payment_id IS NOT NULL;

-- Append-only enforcement
CREATE OR REPLACE FUNCTION fn_fee_comp_no_mutate() RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'fee_computations is append-only' USING ERRCODE = 'P0002';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fee_comp_no_update ON fee_computations;
CREATE TRIGGER trg_fee_comp_no_update
  BEFORE UPDATE ON fee_computations
  FOR EACH ROW EXECUTE FUNCTION fn_fee_comp_no_mutate();

DROP TRIGGER IF EXISTS trg_fee_comp_no_delete ON fee_computations;
CREATE TRIGGER trg_fee_comp_no_delete
  BEFORE DELETE ON fee_computations
  FOR EACH ROW EXECUTE FUNCTION fn_fee_comp_no_mutate();

-- ── 6. updated_at touchers ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_fee_touch_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fee_plans_touch ON fee_plans;
CREATE TRIGGER trg_fee_plans_touch BEFORE UPDATE ON fee_plans
  FOR EACH ROW EXECUTE FUNCTION fn_fee_touch_updated_at();

DROP TRIGGER IF EXISTS trg_fee_rules_touch ON fee_plan_rules;
CREATE TRIGGER trg_fee_rules_touch BEFORE UPDATE ON fee_plan_rules
  FOR EACH ROW EXECUTE FUNCTION fn_fee_touch_updated_at();

DROP TRIGGER IF EXISTS trg_fee_overrides_touch ON merchant_fee_overrides;
CREATE TRIGGER trg_fee_overrides_touch BEFORE UPDATE ON merchant_fee_overrides
  FOR EACH ROW EXECUTE FUNCTION fn_fee_touch_updated_at();

-- ── 7. fn_resolve_fee_plan ────────────────────────────────────────────
-- Returns the plan_id effective for `merchant_id` at `at_ts`.
-- Precedence: active override window → default plan → NULL.
CREATE OR REPLACE FUNCTION fn_resolve_fee_plan(
  p_merchant_id UUID,
  p_at_ts TIMESTAMPTZ DEFAULT now()
) RETURNS BIGINT AS $$
DECLARE
  v_plan_id BIGINT;
BEGIN
  SELECT o.plan_id INTO v_plan_id
    FROM merchant_fee_overrides o
    JOIN fee_plans p ON p.id = o.plan_id
   WHERE o.merchant_id = p_merchant_id
     AND o.valid_from <= p_at_ts
     AND (o.valid_to IS NULL OR o.valid_to > p_at_ts)
     AND p.is_active
   ORDER BY o.valid_from DESC
   LIMIT 1;
  IF v_plan_id IS NOT NULL THEN
    RETURN v_plan_id;
  END IF;

  SELECT id INTO v_plan_id
    FROM fee_plans
   WHERE is_default = true AND is_active = true
   LIMIT 1;
  RETURN v_plan_id;
END;
$$ LANGUAGE plpgsql STABLE;

-- ── 8. fn_compute_fee ─────────────────────────────────────────────────
-- Picks best rule (highest priority, matching method/source, gross in band),
-- computes fee inclusive of GST so that fee + gst == total_deduction to paisa.
CREATE OR REPLACE FUNCTION fn_compute_fee(
  p_merchant_id UUID,
  p_gross NUMERIC,
  p_payment_method TEXT DEFAULT NULL,
  p_order_source TEXT DEFAULT NULL,
  p_currency CHAR(3) DEFAULT 'INR',
  p_at_ts TIMESTAMPTZ DEFAULT now(),
  p_record BOOLEAN DEFAULT false,
  p_payment_id TEXT DEFAULT NULL
) RETURNS JSONB AS $$
DECLARE
  v_plan_id BIGINT;
  v_plan fee_plans%ROWTYPE;
  v_rule fee_plan_rules%ROWTYPE;
  v_gross NUMERIC(14,2) := round(p_gross::numeric, 2);
  v_total_ded NUMERIC(14,2);
  v_fee NUMERIC(14,2);
  v_gst NUMERIC(14,2);
  v_net NUMERIC(14,2);
  v_breakdown JSONB;
  v_comp_id BIGINT;
BEGIN
  IF v_gross < 0 THEN
    RAISE EXCEPTION 'gross must be >= 0' USING ERRCODE = 'P0001';
  END IF;

  v_plan_id := fn_resolve_fee_plan(p_merchant_id, p_at_ts);
  IF v_plan_id IS NULL THEN
    RAISE EXCEPTION 'no fee plan resolved for merchant %', p_merchant_id
      USING ERRCODE = 'P0002';
  END IF;
  SELECT * INTO v_plan FROM fee_plans WHERE id = v_plan_id;

  SELECT * INTO v_rule
    FROM fee_plan_rules
   WHERE plan_id = v_plan_id
     AND is_active
     AND (payment_method IS NULL OR payment_method = p_payment_method)
     AND (order_source IS NULL OR order_source = p_order_source)
     AND v_gross >= min_amount
     AND (max_amount IS NULL OR v_gross < max_amount)
   ORDER BY
     -- specificity: matching method/source beats wildcard
     (CASE WHEN payment_method IS NOT NULL THEN 1 ELSE 0 END
      + CASE WHEN order_source IS NOT NULL THEN 1 ELSE 0 END) DESC,
     priority DESC,
     id ASC
   LIMIT 1;

  IF NOT FOUND THEN
    -- No rule: zero fee, no GST. Caller should add a wildcard fallback rule.
    v_total_ded := 0;
    v_fee := 0;
    v_gst := 0;
    v_net := v_gross;
    v_breakdown := jsonb_build_object(
      'matched', false,
      'reason', 'no_matching_rule'
    );
  ELSE
    -- Compute total deduction first, then derive fee + gst so they sum exactly.
    IF v_rule.fee_type = 'percent' THEN
      v_total_ded := round(v_gross * v_rule.percent_rate, 2);
    ELSIF v_rule.fee_type = 'flat' THEN
      v_total_ded := round(v_rule.flat_fee, 2);
    ELSE -- percent_plus_flat
      v_total_ded := round(v_gross * v_rule.percent_rate + v_rule.flat_fee, 2);
    END IF;
    -- Cap deduction at gross
    IF v_total_ded > v_gross THEN
      v_total_ded := v_gross;
    END IF;
    v_fee := round(v_total_ded / (1 + v_plan.gst_rate), 2);
    v_gst := v_total_ded - v_fee;
    v_net := v_gross - v_total_ded;
    v_breakdown := jsonb_build_object(
      'matched', true,
      'fee_type', v_rule.fee_type,
      'percent_rate', v_rule.percent_rate,
      'flat_fee', v_rule.flat_fee,
      'gst_rate', v_plan.gst_rate
    );
  END IF;

  IF p_record THEN
    INSERT INTO fee_computations
      (merchant_id, payment_id, plan_id, rule_id, payment_method, order_source,
       currency, gross_amount, fee_amount, gst_amount, total_deduction, net_amount,
       breakdown)
    VALUES
      (p_merchant_id, p_payment_id, v_plan_id, v_rule.id, p_payment_method, p_order_source,
       p_currency, v_gross, v_fee, v_gst, v_total_ded, v_net,
       v_breakdown)
    RETURNING id INTO v_comp_id;
  END IF;

  RETURN jsonb_build_object(
    'merchant_id',     p_merchant_id,
    'plan_id',         v_plan_id,
    'plan_code',       v_plan.code,
    'rule_id',         v_rule.id,
    'currency',        p_currency,
    'payment_method',  p_payment_method,
    'order_source',    p_order_source,
    'gross_amount',    v_gross,
    'fee_amount',      v_fee,
    'gst_amount',      v_gst,
    'total_deduction', v_total_ded,
    'net_amount',      v_net,
    'gst_rate',        v_plan.gst_rate,
    'breakdown',       v_breakdown,
    'computation_id',  v_comp_id,
    'computed_at',     p_at_ts
  );
END;
$$ LANGUAGE plpgsql;

-- ── 9. Seed default plan ──────────────────────────────────────────────
-- Mirrors legacy 0.30% headline (fee 0.2542% + 18% GST ⇒ 0.30%).
INSERT INTO fee_plans (code, name, description, gst_rate, is_default, is_active)
VALUES (
  'default_v1',
  'Default Standard Plan',
  'Legacy 0.30%% all-in deduction (split into 0.2542%% fee + 18%% GST on fee)',
  0.180000, true, true
) ON CONFLICT (code) DO NOTHING;

-- Wildcard 0.30% all-in (fee_engine derives fee=0.2542%% + GST=0.0458%%)
INSERT INTO fee_plan_rules
  (plan_id, payment_method, order_source, min_amount, max_amount,
   fee_type, percent_rate, flat_fee, priority, is_active)
SELECT p.id, NULL, NULL, 0, NULL, 'percent', 0.003000, 0, 0, true
  FROM fee_plans p WHERE p.code = 'default_v1'
   AND NOT EXISTS (
     SELECT 1 FROM fee_plan_rules r WHERE r.plan_id = p.id
   );

-- Cash bypass (zero fee on cash)
INSERT INTO fee_plan_rules
  (plan_id, payment_method, order_source, min_amount, max_amount,
   fee_type, percent_rate, flat_fee, priority, is_active)
SELECT p.id, 'cash', NULL, 0, NULL, 'percent', 0, 0, 100, true
  FROM fee_plans p WHERE p.code = 'default_v1'
   AND NOT EXISTS (
     SELECT 1 FROM fee_plan_rules r
      WHERE r.plan_id = p.id AND r.payment_method = 'cash'
   );

-- ── 10. Permissions ───────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
  ('fee_plans.read'),
  ('fee_plans.write'),
  ('fee_plans.read.all'),
  ('fee_plans.admin')
ON CONFLICT (key) DO NOTHING;

-- owner+manager: read; owner: also can preview (read covers it)
INSERT INTO role_permissions (role_id, permission_id, allowed)
SELECT r.id, p.id, true
  FROM roles r
  JOIN permissions p ON p.key = 'fee_plans.read'
 WHERE r.name IN ('owner', 'manager')
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
