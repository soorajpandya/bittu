-- Migration 061 — Grant razorpay.* permissions to existing branch roles
--
-- Migration 060 minted the razorpay.* permission keys but never wired them
-- to any role, so every Razorpay-namespaced endpoint (e.g. GET
-- /payment-intents/{order_id}) returns 403 "Permission denied:
-- razorpay.orders.read" for legitimate POS/owner clients. This migration
-- repairs that by inserting role_permissions rows for the standard
-- per-branch roles seeded in migration 013.
--
-- Grant matrix:
--   owner, manager  → full razorpay.* (read + write + admin)
--   cashier         → read everything + capture + create orders/qrs/refunds
--                     (everything a register operator does at the till)
--   waiter, kitchen,
--   staff           → read-only on orders, qr, payments
--                     (so the floor app can poll payment_status without
--                      being able to issue refunds or capture)

BEGIN;

-- ── 0. Ensure every sub_branch has the full standard role set ───────────
-- Migration 013 seeded roles per sub_branch, but any branch created after
-- that migration ran (or any branch whose seed transaction failed) ends
-- up missing rows in `roles`. This re-runs the seed idempotently.

INSERT INTO roles (name, branch_id, is_default)
SELECT role_name, sb.id, role_name IN ('owner','manager','cashier','waiter','kitchen','staff')
FROM sub_branches sb
CROSS JOIN (VALUES ('owner'), ('manager'), ('cashier'), ('waiter'), ('kitchen'), ('staff')) AS r(role_name)
ON CONFLICT (branch_id, name) DO NOTHING;


-- ── 1. Grant razorpay.* permissions to those roles ─────────────────────

WITH role_perm(role_name, perm_key, allowed) AS (
    VALUES
        -- ── owner ─────────────────────────────────────────────────────────
        ('owner', 'razorpay.orders.read',          true),
        ('owner', 'razorpay.orders.write',         true),
        ('owner', 'razorpay.payments.read',        true),
        ('owner', 'razorpay.payments.capture',     true),
        ('owner', 'razorpay.qr.read',              true),
        ('owner', 'razorpay.qr.write',             true),
        ('owner', 'razorpay.refunds.read',         true),
        ('owner', 'razorpay.refunds.write',        true),
        ('owner', 'razorpay.disputes.read',        true),
        ('owner', 'razorpay.disputes.write',       true),
        ('owner', 'razorpay.settlements.read',     true),
        ('owner', 'razorpay.route.read',           true),
        ('owner', 'razorpay.route.write',          true),
        ('owner', 'razorpay.route.admin',          true),
        ('owner', 'razorpay.smart_collect.read',   true),
        ('owner', 'razorpay.smart_collect.write',  true),
        ('owner', 'razorpay.invoices.read',        true),
        ('owner', 'razorpay.invoices.write',       true),
        ('owner', 'razorpay.recon.read',           true),
        ('owner', 'razorpay.recon.run',            true),
        ('owner', 'razorpay.admin',                true),

        -- ── manager ───────────────────────────────────────────────────────
        ('manager', 'razorpay.orders.read',          true),
        ('manager', 'razorpay.orders.write',         true),
        ('manager', 'razorpay.payments.read',        true),
        ('manager', 'razorpay.payments.capture',     true),
        ('manager', 'razorpay.qr.read',              true),
        ('manager', 'razorpay.qr.write',             true),
        ('manager', 'razorpay.refunds.read',         true),
        ('manager', 'razorpay.refunds.write',        true),
        ('manager', 'razorpay.disputes.read',        true),
        ('manager', 'razorpay.disputes.write',       true),
        ('manager', 'razorpay.settlements.read',     true),
        ('manager', 'razorpay.route.read',           true),
        ('manager', 'razorpay.route.write',          true),
        ('manager', 'razorpay.smart_collect.read',   true),
        ('manager', 'razorpay.smart_collect.write',  true),
        ('manager', 'razorpay.invoices.read',        true),
        ('manager', 'razorpay.invoices.write',       true),
        ('manager', 'razorpay.recon.read',           true),
        ('manager', 'razorpay.recon.run',            true),

        -- ── cashier ───────────────────────────────────────────────────────
        ('cashier', 'razorpay.orders.read',          true),
        ('cashier', 'razorpay.orders.write',         true),
        ('cashier', 'razorpay.payments.read',        true),
        ('cashier', 'razorpay.payments.capture',     true),
        ('cashier', 'razorpay.qr.read',              true),
        ('cashier', 'razorpay.qr.write',             true),
        ('cashier', 'razorpay.refunds.read',         true),
        ('cashier', 'razorpay.refunds.write',        true),
        ('cashier', 'razorpay.invoices.read',        true),
        ('cashier', 'razorpay.settlements.read',     true),

        -- ── waiter ────────────────────────────────────────────────────────
        -- Floor staff need to poll payment status after presenting QR.
        ('waiter', 'razorpay.orders.read',           true),
        ('waiter', 'razorpay.payments.read',         true),
        ('waiter', 'razorpay.qr.read',               true),

        -- ── kitchen ───────────────────────────────────────────────────────
        ('kitchen', 'razorpay.orders.read',          true),
        ('kitchen', 'razorpay.payments.read',        true),

        -- ── staff (generic) ───────────────────────────────────────────────
        ('staff', 'razorpay.orders.read',            true),
        ('staff', 'razorpay.payments.read',          true),
        ('staff', 'razorpay.qr.read',                true)
),
deduped AS (
    SELECT DISTINCT ON (role_name, perm_key)
        role_name, perm_key, allowed
    FROM role_perm
    ORDER BY role_name, perm_key
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, d.allowed, '{}'::jsonb
FROM deduped d
JOIN roles r        ON lower(r.name) = lower(d.role_name)
JOIN permissions p  ON p.key = d.perm_key
ON CONFLICT (role_id, permission_id)
DO UPDATE
SET allowed    = EXCLUDED.allowed,
    updated_at = NOW();

COMMIT;
