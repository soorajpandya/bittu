-- ============================================================================
-- 048_audit_log_append_only.sql
-- ----------------------------------------------------------------------------
-- Make `audit_log` (and `audit_events` if present) genuinely append-only at
-- the storage layer. Service code that already only writes via
-- `app.core.audit_logger.audit_event()` is unaffected; any rogue UPDATE/DELETE
-- — accidental or malicious — is rejected.
--
-- WHY: An audit log that can be silently mutated is not an audit log.
-- This is required for fintech evidentiary value (RBI/PCI investigations).
--
-- SAFE TO RE-RUN: All triggers use CREATE OR REPLACE / DROP IF EXISTS.
-- ============================================================================

BEGIN;

CREATE OR REPLACE FUNCTION fn_audit_block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'audit log is append-only (DELETE denied on %)', TG_TABLE_NAME;
    ELSIF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'audit log is append-only (UPDATE denied on %)', TG_TABLE_NAME;
    END IF;
    RETURN NULL;
END $$;

-- audit_log -------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'audit_log') THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_audit_log_no_update ON audit_log';
        EXECUTE 'DROP TRIGGER IF EXISTS trg_audit_log_no_delete ON audit_log';
        EXECUTE 'CREATE TRIGGER trg_audit_log_no_update BEFORE UPDATE ON audit_log
                 FOR EACH ROW EXECUTE FUNCTION fn_audit_block_mutation()';
        EXECUTE 'CREATE TRIGGER trg_audit_log_no_delete BEFORE DELETE ON audit_log
                 FOR EACH ROW EXECUTE FUNCTION fn_audit_block_mutation()';
    END IF;
END $$;

-- audit_events (if migration 042 created it) ----------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'audit_events') THEN
        EXECUTE 'DROP TRIGGER IF EXISTS trg_audit_events_no_update ON audit_events';
        EXECUTE 'DROP TRIGGER IF EXISTS trg_audit_events_no_delete ON audit_events';
        EXECUTE 'CREATE TRIGGER trg_audit_events_no_update BEFORE UPDATE ON audit_events
                 FOR EACH ROW EXECUTE FUNCTION fn_audit_block_mutation()';
        EXECUTE 'CREATE TRIGGER trg_audit_events_no_delete BEFORE DELETE ON audit_events
                 FOR EACH ROW EXECUTE FUNCTION fn_audit_block_mutation()';
    END IF;
END $$;

COMMIT;
