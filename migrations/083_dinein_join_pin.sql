-- ============================================================================
-- 083_dinein_join_pin.sql
-- ----------------------------------------------------------------------------
-- Device-binding for QR dine-in sessions.
--
-- WHY: A printed table QR only carries restaurant_id + table_id (public). To
--   stop a *different* device from silently joining an active session (e.g. a
--   nearby attacker, or a saved URL opened elsewhere), the first device that
--   opens a session becomes the owner and additional devices must supply a
--   short per-session PIN to join. The PIN is shown to the owner device on
--   scan and can be shared with genuine table-mates.
--
--   join_pin is a short numeric code, unique only within the lifetime of one
--   active session (rotated/regenerated per visit). Nullable so legacy rows
--   and cash/POS-created sessions are unaffected.
-- ============================================================================

ALTER TABLE dine_in_sessions
    ADD COLUMN IF NOT EXISTS join_pin VARCHAR(12);
