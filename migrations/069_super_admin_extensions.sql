-- 069_super_admin_extensions.sql
-- Burptech Super Admin: track manual scheduler triggers so the admin UI
-- can show "last manual run" history. Auto-scheduler ticks are NOT logged
-- here (those go to stdout / structured logs).
--
-- This migration is IDEMPOTENT — safe to re-run.

BEGIN;

CREATE TABLE IF NOT EXISTS super_admin_scheduler_runs (
    id                  BIGSERIAL    PRIMARY KEY,
    scheduler_name      TEXT         NOT NULL,
    triggered_by        UUID         NOT NULL,
    triggered_by_email  TEXT,
    started_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT         NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running','success','failed')),
    result              JSONB,
    error               TEXT
);

CREATE INDEX IF NOT EXISTS ix_super_admin_scheduler_runs_name_started
    ON super_admin_scheduler_runs (scheduler_name, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_super_admin_scheduler_runs_status
    ON super_admin_scheduler_runs (status, started_at DESC)
    WHERE status <> 'success';

COMMIT;
