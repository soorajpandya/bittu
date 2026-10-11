-- Admin onboarding aggregates are separate from merchant subscription onboarding.
BEGIN;
CREATE TABLE IF NOT EXISTS onboarding_grants (
 actor_id uuid NOT NULL, tenant_id uuid NOT NULL, permissions text[] NOT NULL DEFAULT '{}',
 PRIMARY KEY(actor_id,tenant_id)
);
CREATE TABLE IF NOT EXISTS onboarding_sessions (
 token_hash text PRIMARY KEY, actor_id uuid NOT NULL, tenant_id uuid NOT NULL,
 csrf_hash text NOT NULL, expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS onboarding_records (
 restaurant_id uuid PRIMARY KEY REFERENCES restaurants(id), tenant_id uuid NOT NULL,
 status text NOT NULL DEFAULT 'Draft' CHECK(status IN ('Draft','Submitted','Under review','Changes requested','Approved','Active','Rejected')),
 revision bigint NOT NULL DEFAULT 1, policy_version text NOT NULL,
 payload bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), updated_by uuid NOT NULL
);
CREATE INDEX IF NOT EXISTS onboarding_records_tenant ON onboarding_records(tenant_id,restaurant_id);
CREATE TABLE IF NOT EXISTS onboarding_resources (
 id uuid PRIMARY KEY, restaurant_id uuid NOT NULL REFERENCES onboarding_records(restaurant_id),
 kind text NOT NULL, payload bytea NOT NULL, content_cipher bytea,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS onboarding_resources_parent ON onboarding_resources(restaurant_id,kind,id);
CREATE TABLE IF NOT EXISTS onboarding_audit (
 id bigserial PRIMARY KEY, restaurant_id uuid NOT NULL REFERENCES onboarding_records(restaurant_id),
 actor_id uuid NOT NULL, action text NOT NULL, revision bigint NOT NULL,
 details jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS onboarding_snapshots (
 id uuid PRIMARY KEY, restaurant_id uuid NOT NULL REFERENCES onboarding_records(restaurant_id),
 revision bigint NOT NULL, payload bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS onboarding_outbox (
 id bigserial PRIMARY KEY, restaurant_id uuid NOT NULL, event jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), delivered_at timestamptz
);
CREATE TABLE IF NOT EXISTS onboarding_idempotency (
 actor_id uuid NOT NULL, tenant_id uuid NOT NULL, operation text NOT NULL, key text NOT NULL,
 request_hash text NOT NULL, response bytea NOT NULL, status_code int NOT NULL,
 revision bigint, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(actor_id,tenant_id,operation,key)
);
CREATE TABLE IF NOT EXISTS onboarding_menu_links (
 restaurant_id uuid NOT NULL REFERENCES onboarding_records(restaurant_id),
 item_id integer NOT NULL REFERENCES items("Item_ID") ON DELETE CASCADE,
 dietary text NOT NULL CHECK(dietary IN ('veg','non-veg','vegan')),
 PRIMARY KEY(restaurant_id,item_id)
);
-- No client/Supabase REST access; only backend DB role may access these tables.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['onboarding_grants','onboarding_sessions','onboarding_records','onboarding_resources',
 'onboarding_audit','onboarding_snapshots','onboarding_outbox','onboarding_idempotency','onboarding_menu_links'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
 END LOOP;
END $$;
CREATE OR REPLACE FUNCTION onboarding_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'onboarding history is append-only'; END $$;
DROP TRIGGER IF EXISTS onboarding_audit_immutable ON onboarding_audit;
CREATE TRIGGER onboarding_audit_immutable BEFORE UPDATE OR DELETE ON onboarding_audit FOR EACH ROW EXECUTE FUNCTION onboarding_immutable();
DROP TRIGGER IF EXISTS onboarding_snapshots_immutable ON onboarding_snapshots;
CREATE TRIGGER onboarding_snapshots_immutable BEFORE UPDATE OR DELETE ON onboarding_snapshots FOR EACH ROW EXECUTE FUNCTION onboarding_immutable();
-- Block legacy restaurant/menu mutations from bypassing managed review locks.
CREATE OR REPLACE FUNCTION onboarding_guard_legacy() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE rid uuid; managed boolean; BEGIN
 IF current_setting('app.onboarding_write',true) = 'on' THEN
  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
 END IF;
 IF TG_TABLE_NAME='restaurants' THEN rid := OLD.id;
 ELSE
  IF TG_OP='DELETE' THEN rid := OLD.restaurant_id; ELSE rid := NEW.restaurant_id; END IF;
 END IF;
 SELECT EXISTS(SELECT 1 FROM onboarding_records WHERE restaurant_id=rid AND status <> 'Active') INTO managed;
 IF managed THEN RAISE EXCEPTION 'Restaurant is in admin onboarding; use onboarding API' USING ERRCODE='23514'; END IF;
 -- Active partner legal/profile changes require an explicit change-review policy.
 IF TG_TABLE_NAME='restaurants' AND EXISTS(SELECT 1 FROM onboarding_records WHERE restaurant_id=rid)
 THEN RAISE EXCEPTION 'Managed partner profile requires change review' USING ERRCODE='23514'; END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;
DROP TRIGGER IF EXISTS onboarding_restaurant_guard ON restaurants;
CREATE TRIGGER onboarding_restaurant_guard BEFORE UPDATE OR DELETE ON restaurants FOR EACH ROW EXECUTE FUNCTION onboarding_guard_legacy();
DROP TRIGGER IF EXISTS onboarding_item_guard ON items;
CREATE TRIGGER onboarding_item_guard BEFORE INSERT OR UPDATE OR DELETE ON items FOR EACH ROW EXECUTE FUNCTION onboarding_guard_legacy();
COMMIT;
