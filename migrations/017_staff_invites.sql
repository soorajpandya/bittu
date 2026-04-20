-- Migration 017: Staff invite system
-- Allows owners to invite staff by email. When the invited user logs in
-- via Google, the backend auto-links them to the branch with the correct role.
BEGIN;

CREATE TABLE IF NOT EXISTS staff_invites (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id   UUID NOT NULL REFERENCES sub_branches(id) ON DELETE CASCADE,
    owner_id    TEXT NOT NULL,                       -- owner who created the invite
    email       VARCHAR(255) NOT NULL,               -- invited staff email (lowercased)
    role        VARCHAR(50) NOT NULL,                -- manager, cashier, chef, waiter, staff
    role_id     UUID REFERENCES roles(id) ON DELETE SET NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending | accepted | revoked | expired
    accepted_at TIMESTAMP WITH TIME ZONE,
    expires_at  TIMESTAMP WITH TIME ZONE DEFAULT (NOW() + INTERVAL '30 days'),
    created_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW()
);

-- One pending invite per email per branch
CREATE UNIQUE INDEX IF NOT EXISTS uq_staff_invites_pending
    ON staff_invites (branch_id, lower(email))
    WHERE status = 'pending';

-- Fast lookup during login auto-link
CREATE INDEX IF NOT EXISTS idx_staff_invites_email_pending
    ON staff_invites (lower(email))
    WHERE status = 'pending';

-- Add invite permissions
INSERT INTO permissions (key) VALUES
    ('staff.invites.create'),
    ('staff.invites.read'),
    ('staff.invites.revoke')
ON CONFLICT (key) DO NOTHING;

-- Grant invite permissions to owner and manager roles
WITH role_perm(role_name, perm_key, allowed, meta) AS (
    VALUES
    ('owner',   'staff.invites.create', true, '{}'::jsonb),
    ('owner',   'staff.invites.read',   true, '{}'::jsonb),
    ('owner',   'staff.invites.revoke', true, '{}'::jsonb),
    ('manager', 'staff.invites.read',   true, '{}'::jsonb)
)
INSERT INTO role_permissions (role_id, permission_id, allowed, meta)
SELECT r.id, p.id, rp.allowed, rp.meta
FROM role_perm rp
JOIN permissions p ON p.key = rp.perm_key
JOIN roles r ON lower(r.name) = lower(rp.role_name)
ON CONFLICT (role_id, permission_id) DO NOTHING;

COMMIT;
