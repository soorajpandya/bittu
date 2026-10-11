BEGIN;
-- Apply after 087. Link both session engines by immutable visit token + table.
ALTER TABLE dine_in_sessions ADD COLUMN IF NOT EXISTS legacy_session_id uuid REFERENCES table_sessions(id);
CREATE UNIQUE INDEX IF NOT EXISTS dinein_legacy_visit ON dine_in_sessions(legacy_session_id) WHERE legacy_session_id IS NOT NULL;
UPDATE dine_in_sessions d SET legacy_session_id=s.id
FROM table_sessions s WHERE d.table_id=s.table_id AND d.session_token=s.session_token
AND d.legacy_session_id IS NULL
AND (SELECT count(*) FROM dine_in_sessions x WHERE x.table_id=s.table_id AND x.session_token=s.session_token)=1;

CREATE OR REPLACE FUNCTION waitlist_session_branch() RETURNS trigger AS $$
BEGIN
  SELECT branch_id INTO NEW.branch_id FROM restaurant_tables WHERE id=NEW.table_id;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS session_branch ON table_sessions;
CREATE TRIGGER session_branch BEFORE INSERT ON table_sessions FOR EACH ROW EXECUTE FUNCTION waitlist_session_branch();
DROP TRIGGER IF EXISTS session_branch ON dine_in_sessions;
CREATE TRIGGER session_branch BEFORE INSERT ON dine_in_sessions FOR EACH ROW EXECUTE FUNCTION waitlist_session_branch();
UPDATE table_sessions s SET branch_id=t.branch_id FROM restaurant_tables t WHERE s.table_id=t.id AND s.branch_id IS DISTINCT FROM t.branch_id;
UPDATE dine_in_sessions s SET branch_id=t.branch_id FROM restaurant_tables t WHERE s.table_id=t.id AND s.branch_id IS DISTINCT FROM t.branch_id;

-- Existing waitlist seats also need a canonical session before their first order.
INSERT INTO dine_in_sessions(id,legacy_session_id,table_id,restaurant_id,user_id,branch_id,session_token,guest_count,status,expires_at,join_pin)
SELECT s.id,s.id,s.table_id,s.restaurant_id,s.user_id,s.branch_id,s.session_token,s.guest_count,'active',s.expires_at,
       lpad(floor(random()*10000)::int::text,4,'0')
FROM table_sessions s WHERE s.is_active AND (s.expires_at IS NULL OR s.expires_at>now())
AND EXISTS(SELECT 1 FROM waitlist_entries w WHERE w.seated_session_id=s.id AND w.status='seated')
AND NOT EXISTS(SELECT 1 FROM dine_in_sessions d WHERE d.table_id=s.table_id AND (d.status='active' OR d.session_token=s.session_token))
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION close_linked_table_visit() RETURNS trigger AS $$
BEGIN
  IF TG_TABLE_NAME='dine_in_sessions' THEN
    IF OLD.status='active' AND NEW.status<>'active' THEN
      UPDATE table_sessions SET is_active=false,status='completed',ended_at=COALESCE(NEW.ended_at,now())
      WHERE table_id=NEW.table_id AND session_token=NEW.session_token AND is_active;
    END IF;
  ELSE
    IF OLD.is_active AND NOT NEW.is_active THEN
      UPDATE dine_in_sessions SET status='completed',ended_at=COALESCE(NEW.ended_at,now())
      WHERE table_id=NEW.table_id AND session_token=NEW.session_token AND status='active';
    END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS close_linked_visit ON table_sessions;
CREATE TRIGGER close_linked_visit AFTER UPDATE ON table_sessions FOR EACH ROW EXECUTE FUNCTION close_linked_table_visit();
DROP TRIGGER IF EXISTS close_linked_visit ON dine_in_sessions;
CREATE TRIGGER close_linked_visit AFTER UPDATE ON dine_in_sessions FOR EACH ROW EXECUTE FUNCTION close_linked_table_visit();
-- Repair the historical paid-but-active legacy rows, without touching a later visit.
UPDATE table_sessions s SET is_active=false,status='completed',ended_at=now()
WHERE s.is_active AND EXISTS (SELECT 1 FROM dine_in_sessions d WHERE d.table_id=s.table_id AND d.session_token=s.session_token AND d.status<>'active');

ALTER TABLE waitlist_entries ADD COLUMN IF NOT EXISTS push_token_hash text;
CREATE TABLE IF NOT EXISTS waitlist_push_jobs (
 entry_id uuid PRIMARY KEY REFERENCES waitlist_entries(id) ON DELETE CASCADE,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','sending','delivered','failed','cancelled')),
 attempts int NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz,
 lease_token uuid,
 delivered_at timestamptz,
 last_error text
);
CREATE INDEX IF NOT EXISTS waitlist_push_due ON waitlist_push_jobs(next_attempt_at) WHERE state IN ('pending','sending');
-- Override hosted-database default grants: the outbox is server-only.
REVOKE ALL ON waitlist_push_jobs FROM PUBLIC;
DO $$
DECLARE client_role text;
BEGIN
 FOREACH client_role IN ARRAY ARRAY['anon','authenticated'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=client_role) THEN
   EXECUTE format('REVOKE ALL ON waitlist_push_jobs FROM %I',client_role);
  END IF;
 END LOOP;
END; $$;
-- Contains no public credentials. Access is through the server only.
ALTER TABLE waitlist_push_jobs ENABLE ROW LEVEL SECURITY;
-- Match the backend tenant context used by migration 049. Only server roles
-- receive table privileges; no grants are made to anonymous API roles.
DROP POLICY IF EXISTS waitlist_push_service ON waitlist_push_jobs;
CREATE POLICY waitlist_push_service ON waitlist_push_jobs FOR ALL
USING (NULLIF(current_setting('app.tenant_id',true),'') IS NULL)
WITH CHECK (NULLIF(current_setting('app.tenant_id',true),'') IS NULL);
DROP POLICY IF EXISTS waitlist_push_owner_read ON waitlist_push_jobs;
CREATE POLICY waitlist_push_owner_read ON waitlist_push_jobs FOR SELECT
USING (EXISTS(SELECT 1 FROM waitlist_entries w WHERE w.id=entry_id AND w.user_id=current_setting('app.tenant_id',true)));
CREATE OR REPLACE FUNCTION enqueue_waitlist_push() RETURNS trigger AS $$
BEGIN
 IF NEW.status='notified' AND OLD.status IS DISTINCT FROM NEW.status THEN
   INSERT INTO waitlist_push_jobs(entry_id) VALUES(NEW.id)
   ON CONFLICT(entry_id) DO UPDATE SET state='pending',attempts=0,next_attempt_at=now(),lease_until=NULL,lease_token=NULL;
 ELSIF NEW.status IN ('seated','cancelled','skipped') THEN
   UPDATE waitlist_push_jobs SET state='cancelled',lease_token=NULL WHERE entry_id=NEW.id AND state IN ('pending','sending');
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql SECURITY DEFINER SET search_path FROM CURRENT;
DROP TRIGGER IF EXISTS waitlist_push_enqueue ON waitlist_entries;
CREATE TRIGGER waitlist_push_enqueue AFTER UPDATE OF status ON waitlist_entries FOR EACH ROW EXECUTE FUNCTION enqueue_waitlist_push();
INSERT INTO waitlist_push_jobs(entry_id)
SELECT id FROM waitlist_entries WHERE status='notified' AND expires_at>now()
ON CONFLICT DO NOTHING;
UPDATE waitlist_settings SET sms_enabled=false,whatsapp_enabled=false;
ALTER TABLE waitlist_settings DROP CONSTRAINT IF EXISTS waitlist_supported_channels;
ALTER TABLE waitlist_settings ADD CONSTRAINT waitlist_supported_channels CHECK(NOT sms_enabled AND NOT whatsapp_enabled);

COMMIT;
