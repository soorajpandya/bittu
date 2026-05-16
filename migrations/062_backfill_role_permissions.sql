-- Migration 062 — Backfill role_permissions across all sub_branches
--
-- Migrations 013/014/015/016/017 (and friends) seed role_permissions per
-- (branch, role, permission). Their seeds are written as
--   JOIN roles r ON lower(r.name) = lower(d.role_name)
-- which means a sub_branch that was created AFTER those migrations ran
-- gets the role rows (re-seeded by 061's idempotent insert) but inherits
-- ZERO permission grants — leading to runtime 403s like
-- "Permission denied: menu.read", "order.create", "waitlist.read", etc.
--
-- Rather than re-import every historical seed VALUES list (brittle and
-- duplicates source-of-truth), this migration replicates the union of
-- existing (role_name, permission, allowed=true, meta) grants across
-- every same-named role row. ON CONFLICT DO NOTHING preserves any
-- explicit grants that already exist on a target role.
--
-- Effect: every owner role now has every grant any owner row anywhere
-- has, every cashier has every cashier grant, etc. New sub_branches
-- created after this point should ideally trigger a re-run too — but
-- 062 is safe to re-execute manually any time.

BEGIN;

-- ── 1. Owner gets every permission, unconditionally ────────────────────
-- Owner is the merchant root. There is no permission an owner should be
-- denied. Some legacy branches never had an `owner` role row receive any
-- grants (their owner row was created but the seed never ran), so cross-
-- branch propagation below cannot help them. Grant the full permission
-- catalogue here.

INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, true, '{}'::jsonb
FROM roles r
CROSS JOIN permissions p
WHERE lower(r.name) = 'owner'
ON CONFLICT (role_id, permission_id)
DO UPDATE
SET allowed    = true,
    updated_at = NOW();


-- ── 2. Cross-branch propagation for non-owner roles ────────────────────

INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT
    target.id            AS role_id,
    rp.permission_id     AS permission_id,
    bool_or(rp.allowed)  AS allowed,
    -- Pick any non-null meta; ON CONFLICT branch keeps existing meta anyway.
    COALESCE(
        (array_agg(rp.meta) FILTER (WHERE rp.meta IS NOT NULL))[1],
        '{}'::jsonb
    )                    AS meta
FROM roles target
JOIN roles src
       ON lower(src.name) = lower(target.name)
      AND src.id <> target.id
JOIN role_permissions rp
       ON rp.role_id = src.id
      AND rp.allowed = true
GROUP BY target.id, rp.permission_id
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
