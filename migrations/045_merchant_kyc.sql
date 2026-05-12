-- ╔══════════════════════════════════════════════════════════════════════╗
-- ║ Phase 9 — Merchant KYC & Onboarding                                  ║
-- ║                                                                      ║
-- ║   Self-contained merchant-level KYC engine. Independent of the       ║
-- ║   existing user-level `kyc_verifications` table (Cashfree-driven).   ║
-- ║   No gateway wiring. Documents are stored as opaque URLs + hashes;   ║
-- ║   admin reviews state transitions manually.                          ║
-- ║                                                                      ║
-- ║ Tables:                                                              ║
-- ║   merchant_kyc_profiles          one per merchant                    ║
-- ║   merchant_kyc_documents         file metadata, per-doc status       ║
-- ║   merchant_kyc_owners            UBO / directors / partners          ║
-- ║   merchant_kyc_bank_accounts     account metadata, primary flag      ║
-- ║   merchant_kyc_audit_events      append-only state history           ║
-- ║                                                                      ║
-- ║ Functions:                                                           ║
-- ║   fn_kyc_submit(merchant, actor_user)                                ║
-- ║   fn_kyc_review(merchant, admin, decision, reason)                   ║
-- ║   fn_kyc_set_under_review(merchant, admin)                           ║
-- ║   fn_kyc_suspend(merchant, admin, reason)                            ║
-- ║   fn_kyc_unsuspend(merchant, admin)                                  ║
-- ╚══════════════════════════════════════════════════════════════════════╝

BEGIN;

-- ── enums ───────────────────────────────────────────────────────────────
DO $$ BEGIN
  CREATE TYPE merchant_kyc_status AS ENUM (
    'draft', 'submitted', 'under_review',
    'approved', 'rejected', 'suspended'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE merchant_kyc_doc_type AS ENUM (
    'pan_card', 'gstin_certificate', 'coi', 'moa', 'aoa',
    'address_proof', 'bank_proof',
    'owner_id_proof', 'owner_address_proof',
    'partnership_deed', 'shop_license', 'other'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE merchant_kyc_doc_status AS ENUM (
    'pending', 'verified', 'rejected', 'expired'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE merchant_kyc_owner_role AS ENUM (
    'director', 'partner', 'proprietor', 'ubo', 'authorized_signatory'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE merchant_kyc_business_type AS ENUM (
    'proprietorship', 'partnership', 'llp', 'private_limited',
    'public_limited', 'huf', 'trust', 'society', 'individual', 'other'
  );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 1. profile (one per merchant) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_kyc_profiles (
  merchant_id          UUID         PRIMARY KEY,
  legal_name           TEXT,
  business_type        merchant_kyc_business_type,
  pan                  VARCHAR(10),
  gstin                VARCHAR(15),
  cin                  VARCHAR(21),
  registered_address   JSONB        NOT NULL DEFAULT '{}'::jsonb,
  contact_email        TEXT,
  contact_phone        TEXT,
  website              TEXT,
  status               merchant_kyc_status NOT NULL DEFAULT 'draft',
  risk_tier            VARCHAR(16)  NOT NULL DEFAULT 'standard',
  rejection_reason     TEXT,
  suspension_reason    TEXT,
  submitted_at         TIMESTAMPTZ,
  reviewed_at          TIMESTAMPTZ,
  reviewed_by_admin_id UUID,
  approved_at          TIMESTAMPTZ,
  suspended_at         TIMESTAMPTZ,
  version              INT          NOT NULL DEFAULT 1,
  metadata             JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_merchant_kyc_profiles_status
  ON merchant_kyc_profiles (status);

-- ── 2. documents ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_kyc_documents (
  id                BIGSERIAL    PRIMARY KEY,
  document_uuid     UUID         NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  merchant_id       UUID         NOT NULL,
  owner_id          BIGINT,                       -- nullable: doc may belong to an owner
  doc_type          merchant_kyc_doc_type NOT NULL,
  file_url          TEXT         NOT NULL,
  file_hash         TEXT,
  mime_type         VARCHAR(100),
  size_bytes        BIGINT,
  status            merchant_kyc_doc_status NOT NULL DEFAULT 'pending',
  rejection_reason  TEXT,
  expires_at        TIMESTAMPTZ,
  uploaded_by_user_id   UUID,
  verified_by_admin_id  UUID,
  verified_at       TIMESTAMPTZ,
  metadata          JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_merchant_kyc_documents_merchant
  ON merchant_kyc_documents (merchant_id, doc_type);
CREATE INDEX IF NOT EXISTS ix_merchant_kyc_documents_status
  ON merchant_kyc_documents (status);

-- ── 3. owners / UBOs ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_kyc_owners (
  id                BIGSERIAL    PRIMARY KEY,
  owner_uuid        UUID         NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  merchant_id       UUID         NOT NULL,
  full_name         TEXT         NOT NULL,
  role              merchant_kyc_owner_role NOT NULL,
  dob               DATE,
  pan               VARCHAR(10),
  aadhaar_last4     VARCHAR(4),
  ownership_pct     NUMERIC(5,2) NOT NULL DEFAULT 0,
  email             TEXT,
  phone             TEXT,
  is_signatory      BOOLEAN      NOT NULL DEFAULT false,
  metadata          JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT chk_ownership_pct
    CHECK (ownership_pct >= 0 AND ownership_pct <= 100)
);

CREATE INDEX IF NOT EXISTS ix_merchant_kyc_owners_merchant
  ON merchant_kyc_owners (merchant_id);

-- now that owners exists, add the FK on documents.owner_id
DO $$ BEGIN
  ALTER TABLE merchant_kyc_documents
    ADD CONSTRAINT fk_kyc_doc_owner
    FOREIGN KEY (owner_id) REFERENCES merchant_kyc_owners(id) ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 4. bank accounts ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_kyc_bank_accounts (
  id                    BIGSERIAL    PRIMARY KEY,
  account_uuid          UUID         NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  merchant_id           UUID         NOT NULL,
  account_holder_name   TEXT         NOT NULL,
  account_number_last4  VARCHAR(4)   NOT NULL,
  account_number_hash   TEXT         NOT NULL,
  ifsc                  VARCHAR(11)  NOT NULL,
  bank_name             TEXT,
  branch                TEXT,
  account_type          VARCHAR(32)  NOT NULL DEFAULT 'current',
  is_primary            BOOLEAN      NOT NULL DEFAULT false,
  is_verified           BOOLEAN      NOT NULL DEFAULT false,
  verification_method   VARCHAR(32),       -- 'penny_drop'|'doc'|'manual'
  verification_ref      TEXT,
  verified_by_admin_id  UUID,
  verified_at           TIMESTAMPTZ,
  metadata              JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kyc_bank_merchant
  ON merchant_kyc_bank_accounts (merchant_id);

-- only one primary bank per merchant
CREATE UNIQUE INDEX IF NOT EXISTS uq_kyc_bank_primary
  ON merchant_kyc_bank_accounts (merchant_id)
  WHERE is_primary = true;

-- ── 5. append-only audit events ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS merchant_kyc_audit_events (
  id            BIGSERIAL    PRIMARY KEY,
  merchant_id   UUID         NOT NULL,
  event_type    VARCHAR(64)  NOT NULL,    -- profile.submitted, doc.verified, ...
  from_status   merchant_kyc_status,
  to_status     merchant_kyc_status,
  actor_user_id  UUID,
  actor_admin_id UUID,
  reason        TEXT,
  payload       JSONB        NOT NULL DEFAULT '{}'::jsonb,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_kyc_audit_merchant_time
  ON merchant_kyc_audit_events (merchant_id, created_at DESC);

CREATE OR REPLACE FUNCTION fn_kyc_audit_no_mutate()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION 'merchant_kyc_audit_events is append-only'
    USING ERRCODE = 'P0002';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_kyc_audit_no_update ON merchant_kyc_audit_events;
CREATE TRIGGER trg_kyc_audit_no_update
  BEFORE UPDATE ON merchant_kyc_audit_events
  FOR EACH ROW EXECUTE FUNCTION fn_kyc_audit_no_mutate();

DROP TRIGGER IF EXISTS trg_kyc_audit_no_delete ON merchant_kyc_audit_events;
CREATE TRIGGER trg_kyc_audit_no_delete
  BEFORE DELETE ON merchant_kyc_audit_events
  FOR EACH ROW EXECUTE FUNCTION fn_kyc_audit_no_mutate();

-- ── 6. updated_at touchers ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_kyc_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DO $$ BEGIN
  CREATE TRIGGER trg_kyc_profile_touch BEFORE UPDATE ON merchant_kyc_profiles
    FOR EACH ROW EXECUTE FUNCTION fn_kyc_touch_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TRIGGER trg_kyc_doc_touch BEFORE UPDATE ON merchant_kyc_documents
    FOR EACH ROW EXECUTE FUNCTION fn_kyc_touch_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TRIGGER trg_kyc_owner_touch BEFORE UPDATE ON merchant_kyc_owners
    FOR EACH ROW EXECUTE FUNCTION fn_kyc_touch_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TRIGGER trg_kyc_bank_touch BEFORE UPDATE ON merchant_kyc_bank_accounts
    FOR EACH ROW EXECUTE FUNCTION fn_kyc_touch_updated_at();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── 7. fn_kyc_submit ────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_kyc_submit(
  p_merchant_id UUID,
  p_actor_user_id UUID
) RETURNS JSONB AS $$
DECLARE
  v_profile merchant_kyc_profiles%ROWTYPE;
  v_owner_count INT;
  v_bank_count INT;
  v_pan_doc INT;
  v_addr_doc INT;
  v_bank_doc INT;
  v_missing TEXT[] := ARRAY[]::TEXT[];
BEGIN
  SELECT * INTO v_profile FROM merchant_kyc_profiles
    WHERE merchant_id = p_merchant_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'kyc profile not found' USING ERRCODE = 'P0002';
  END IF;
  IF v_profile.status NOT IN ('draft', 'rejected') THEN
    RAISE EXCEPTION 'cannot submit kyc from status %', v_profile.status
      USING ERRCODE = 'P0001';
  END IF;

  IF v_profile.legal_name IS NULL OR length(trim(v_profile.legal_name)) = 0 THEN
    v_missing := array_append(v_missing, 'legal_name');
  END IF;
  IF v_profile.business_type IS NULL THEN v_missing := array_append(v_missing, 'business_type'); END IF;
  IF v_profile.pan IS NULL OR length(v_profile.pan) <> 10 THEN
    v_missing := array_append(v_missing, 'pan');
  END IF;
  IF v_profile.contact_email IS NULL THEN v_missing := array_append(v_missing, 'contact_email'); END IF;
  IF v_profile.contact_phone IS NULL THEN v_missing := array_append(v_missing, 'contact_phone'); END IF;
  IF v_profile.registered_address = '{}'::jsonb THEN
    v_missing := array_append(v_missing, 'registered_address');
  END IF;

  SELECT COUNT(*) INTO v_owner_count FROM merchant_kyc_owners
    WHERE merchant_id = p_merchant_id;
  IF v_owner_count = 0 THEN v_missing := array_append(v_missing, 'owners'); END IF;

  SELECT COUNT(*) INTO v_bank_count FROM merchant_kyc_bank_accounts
    WHERE merchant_id = p_merchant_id AND is_primary = true;
  IF v_bank_count = 0 THEN v_missing := array_append(v_missing, 'primary_bank_account'); END IF;

  SELECT COUNT(*) INTO v_pan_doc FROM merchant_kyc_documents
    WHERE merchant_id = p_merchant_id AND doc_type = 'pan_card'
      AND status <> 'rejected';
  IF v_pan_doc = 0 THEN v_missing := array_append(v_missing, 'doc:pan_card'); END IF;

  SELECT COUNT(*) INTO v_addr_doc FROM merchant_kyc_documents
    WHERE merchant_id = p_merchant_id AND doc_type = 'address_proof'
      AND status <> 'rejected';
  IF v_addr_doc = 0 THEN v_missing := array_append(v_missing, 'doc:address_proof'); END IF;

  SELECT COUNT(*) INTO v_bank_doc FROM merchant_kyc_documents
    WHERE merchant_id = p_merchant_id AND doc_type = 'bank_proof'
      AND status <> 'rejected';
  IF v_bank_doc = 0 THEN v_missing := array_append(v_missing, 'doc:bank_proof'); END IF;

  IF array_length(v_missing, 1) > 0 THEN
    RAISE EXCEPTION 'kyc submission incomplete: %', array_to_string(v_missing, ',')
      USING ERRCODE = 'P0001';
  END IF;

  UPDATE merchant_kyc_profiles
     SET status = 'submitted',
         submitted_at = now(),
         rejection_reason = NULL,
         version = version + 1
   WHERE merchant_id = p_merchant_id
   RETURNING * INTO v_profile;

  INSERT INTO merchant_kyc_audit_events
    (merchant_id, event_type, from_status, to_status, actor_user_id, payload)
  VALUES (p_merchant_id, 'profile.submitted', 'draft', 'submitted',
          p_actor_user_id,
          jsonb_build_object('owners', v_owner_count, 'banks', v_bank_count));

  RETURN to_jsonb(v_profile);
END;
$$ LANGUAGE plpgsql;

-- ── 8. fn_kyc_set_under_review ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_kyc_set_under_review(
  p_merchant_id UUID,
  p_admin_id UUID
) RETURNS JSONB AS $$
DECLARE v_profile merchant_kyc_profiles%ROWTYPE;
BEGIN
  SELECT * INTO v_profile FROM merchant_kyc_profiles
    WHERE merchant_id = p_merchant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'kyc profile not found' USING ERRCODE = 'P0002'; END IF;
  IF v_profile.status NOT IN ('submitted', 'suspended') THEN
    RAISE EXCEPTION 'cannot move to under_review from %', v_profile.status
      USING ERRCODE = 'P0001';
  END IF;

  UPDATE merchant_kyc_profiles
     SET status = 'under_review',
         reviewed_by_admin_id = p_admin_id
   WHERE merchant_id = p_merchant_id
   RETURNING * INTO v_profile;

  INSERT INTO merchant_kyc_audit_events
    (merchant_id, event_type, from_status, to_status, actor_admin_id)
  VALUES (p_merchant_id, 'profile.under_review',
          (SELECT v_profile.status), 'under_review', p_admin_id);

  RETURN to_jsonb(v_profile);
END;
$$ LANGUAGE plpgsql;

-- ── 9. fn_kyc_review (decision: approve|reject) ─────────────────────────
CREATE OR REPLACE FUNCTION fn_kyc_review(
  p_merchant_id UUID,
  p_admin_id    UUID,
  p_decision    TEXT,
  p_reason      TEXT DEFAULT NULL
) RETURNS JSONB AS $$
DECLARE
  v_profile merchant_kyc_profiles%ROWTYPE;
  v_from merchant_kyc_status;
  v_to   merchant_kyc_status;
BEGIN
  IF p_decision NOT IN ('approve', 'reject') THEN
    RAISE EXCEPTION 'decision must be approve|reject' USING ERRCODE = 'P0001';
  END IF;

  SELECT * INTO v_profile FROM merchant_kyc_profiles
    WHERE merchant_id = p_merchant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'kyc profile not found' USING ERRCODE = 'P0002'; END IF;
  IF v_profile.status NOT IN ('submitted', 'under_review') THEN
    RAISE EXCEPTION 'cannot review from status %', v_profile.status
      USING ERRCODE = 'P0001';
  END IF;
  IF p_decision = 'reject' AND (p_reason IS NULL OR length(trim(p_reason)) = 0) THEN
    RAISE EXCEPTION 'rejection requires reason' USING ERRCODE = 'P0001';
  END IF;

  v_from := v_profile.status;
  v_to   := CASE p_decision WHEN 'approve' THEN 'approved'::merchant_kyc_status
                            ELSE 'rejected'::merchant_kyc_status END;

  UPDATE merchant_kyc_profiles
     SET status = v_to,
         reviewed_at = now(),
         reviewed_by_admin_id = p_admin_id,
         approved_at = CASE WHEN v_to = 'approved' THEN now() ELSE approved_at END,
         rejection_reason = CASE WHEN v_to = 'rejected' THEN p_reason ELSE NULL END
   WHERE merchant_id = p_merchant_id
   RETURNING * INTO v_profile;

  INSERT INTO merchant_kyc_audit_events
    (merchant_id, event_type, from_status, to_status, actor_admin_id, reason)
  VALUES (p_merchant_id,
          CASE p_decision WHEN 'approve' THEN 'profile.approved' ELSE 'profile.rejected' END,
          v_from, v_to, p_admin_id, p_reason);

  RETURN to_jsonb(v_profile);
END;
$$ LANGUAGE plpgsql;

-- ── 10. fn_kyc_suspend / unsuspend ──────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_kyc_suspend(
  p_merchant_id UUID, p_admin_id UUID, p_reason TEXT
) RETURNS JSONB AS $$
DECLARE v_profile merchant_kyc_profiles%ROWTYPE;
BEGIN
  IF p_reason IS NULL OR length(trim(p_reason)) = 0 THEN
    RAISE EXCEPTION 'suspension requires reason' USING ERRCODE = 'P0001';
  END IF;
  SELECT * INTO v_profile FROM merchant_kyc_profiles
    WHERE merchant_id = p_merchant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'kyc profile not found' USING ERRCODE = 'P0002'; END IF;
  IF v_profile.status <> 'approved' THEN
    RAISE EXCEPTION 'can only suspend approved profiles (current=%)', v_profile.status
      USING ERRCODE = 'P0001';
  END IF;

  UPDATE merchant_kyc_profiles
     SET status = 'suspended',
         suspended_at = now(),
         suspension_reason = p_reason,
         reviewed_by_admin_id = p_admin_id
   WHERE merchant_id = p_merchant_id
   RETURNING * INTO v_profile;

  INSERT INTO merchant_kyc_audit_events
    (merchant_id, event_type, from_status, to_status, actor_admin_id, reason)
  VALUES (p_merchant_id, 'profile.suspended', 'approved', 'suspended', p_admin_id, p_reason);

  RETURN to_jsonb(v_profile);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_kyc_unsuspend(
  p_merchant_id UUID, p_admin_id UUID
) RETURNS JSONB AS $$
DECLARE v_profile merchant_kyc_profiles%ROWTYPE;
BEGIN
  SELECT * INTO v_profile FROM merchant_kyc_profiles
    WHERE merchant_id = p_merchant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'kyc profile not found' USING ERRCODE = 'P0002'; END IF;
  IF v_profile.status <> 'suspended' THEN
    RAISE EXCEPTION 'can only unsuspend suspended profiles (current=%)', v_profile.status
      USING ERRCODE = 'P0001';
  END IF;

  UPDATE merchant_kyc_profiles
     SET status = 'approved',
         suspended_at = NULL,
         suspension_reason = NULL,
         reviewed_by_admin_id = p_admin_id
   WHERE merchant_id = p_merchant_id
   RETURNING * INTO v_profile;

  INSERT INTO merchant_kyc_audit_events
    (merchant_id, event_type, from_status, to_status, actor_admin_id)
  VALUES (p_merchant_id, 'profile.unsuspended', 'suspended', 'approved', p_admin_id);

  RETURN to_jsonb(v_profile);
END;
$$ LANGUAGE plpgsql;

-- ── 11. permissions ─────────────────────────────────────────────────────
INSERT INTO permissions (key) VALUES
  ('kyc.read'), ('kyc.write'),
  ('kyc.read.all'), ('kyc.review'), ('kyc.suspend')
ON CONFLICT (key) DO NOTHING;

-- owner: read+write+submit, manager: read only
INSERT INTO role_permissions (role_id, permission_id, allowed)
SELECT r.id, p.id, true
FROM roles r
JOIN permissions p ON p.key IN ('kyc.read', 'kyc.write')
WHERE r.name = 'owner'
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO role_permissions (role_id, permission_id, allowed)
SELECT r.id, p.id, true
FROM roles r
JOIN permissions p ON p.key = 'kyc.read'
WHERE r.name = 'manager'
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
