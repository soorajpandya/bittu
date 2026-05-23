-- ============================================================================
-- 070_refresh_token_rotation.sql
-- ----------------------------------------------------------------------------
-- Server-side refresh-token tracking for rotation + reuse detection.
--
-- Supabase Auth (GoTrue) already issues + rotates refresh tokens; this table
-- gives us a *forensic* shadow of every token we hand back to a device, so we
-- can detect the "stolen refresh token replayed after rotation" pattern and
-- invalidate every active session for that (user, device) pair.
--
-- Tokens are NEVER stored raw — only sha256 hashes. The plaintext lives only
-- in transit and on the client.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id              BIGSERIAL PRIMARY KEY,
    user_id         UUID        NOT NULL,
    device_id       TEXT        NOT NULL,
    token_hash      TEXT        NOT NULL,
    parent_hash     TEXT,                              -- previous token in the chain
    rotated_to      TEXT,                              -- next token in the chain
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoked_reason  TEXT,                              -- rotated | reuse_detected | logout | admin
    ip              TEXT,
    user_agent      TEXT,
    CONSTRAINT uq_refresh_token_hash UNIQUE (token_hash)
);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_device
    ON refresh_tokens(user_id, device_id);

CREATE INDEX IF NOT EXISTS idx_refresh_tokens_active
    ON refresh_tokens(user_id, device_id)
    WHERE revoked_at IS NULL;

-- Helper: revoke every active token for a (user, device) — used when reuse
-- of a rotated token is detected. Returns count of rows revoked.
CREATE OR REPLACE FUNCTION fn_refresh_token_revoke_chain(
    p_user_id   UUID,
    p_device_id TEXT,
    p_reason    TEXT DEFAULT 'reuse_detected'
) RETURNS INTEGER LANGUAGE plpgsql AS $$
DECLARE
    v_count INTEGER;
BEGIN
    UPDATE refresh_tokens
       SET revoked_at = COALESCE(revoked_at, now()),
           revoked_reason = COALESCE(revoked_reason, p_reason)
     WHERE user_id = p_user_id
       AND device_id = p_device_id
       AND revoked_at IS NULL;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$;

COMMIT;
