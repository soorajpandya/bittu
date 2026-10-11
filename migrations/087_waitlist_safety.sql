-- Apply before deploying waitlist safety changes. No customer history is deleted.
BEGIN;
ALTER TABLE restaurant_tables ADD COLUMN IF NOT EXISTS branch_id uuid REFERENCES sub_branches(id);
-- Older table creation did not stamp branch_id. Only infer the restaurant's
-- explicit main branch (or its sole active branch); never guess among peers.
WITH destination AS (
    SELECT restaurant_id,
           CASE WHEN count(*) FILTER (WHERE is_main_branch)=1
                THEN (array_agg(id) FILTER (WHERE is_main_branch))[1]
                WHEN count(*)=1 THEN (array_agg(id))[1] END AS id
    FROM sub_branches WHERE is_active GROUP BY restaurant_id
)
UPDATE restaurant_tables t SET branch_id=d.id FROM destination d
WHERE t.restaurant_id=d.restaurant_id AND t.branch_id IS NULL AND d.id IS NOT NULL;
ALTER TABLE waitlist_entries ADD COLUMN IF NOT EXISTS seated_session_id uuid REFERENCES table_sessions(id);
-- Legacy duplicate assignments are returned to waiting so only the first
-- notified party keeps the table. Hosts can notify the others again normally.
WITH duplicates AS (
    SELECT id,row_number() OVER (PARTITION BY assigned_table_id ORDER BY notified_at NULLS LAST,created_at,id) AS rn
    FROM waitlist_entries WHERE status='notified' AND assigned_table_id IS NOT NULL
)
UPDATE waitlist_entries w SET status='waiting',assigned_table_id=NULL,notified_at=NULL,expires_at=NULL
FROM duplicates d WHERE w.id=d.id AND d.rn>1;
CREATE UNIQUE INDEX IF NOT EXISTS waitlist_one_notified_party_per_table
    ON waitlist_entries(assigned_table_id) WHERE status='notified' AND assigned_table_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS waitlist_location_queue
    ON waitlist_entries(restaurant_id,branch_id,status,position);
CREATE INDEX IF NOT EXISTS waitlist_notification_expiry
    ON waitlist_entries(expires_at) WHERE status='notified';
-- Preserve the location of already assigned legacy parties.
UPDATE waitlist_entries w SET branch_id=t.branch_id FROM restaurant_tables t
WHERE w.assigned_table_id=t.id AND w.restaurant_id=t.restaurant_id
  AND w.branch_id IS NULL AND t.branch_id IS NOT NULL AND w.status IN ('waiting','notified');
-- Restaurant-only QR codes identify the explicit main/sole active branch.
-- Ambiguous rows are left intact for explicit host assignment before rollout.
WITH destination AS (
    SELECT restaurant_id,
           CASE WHEN count(*) FILTER (WHERE is_main_branch)=1
                THEN (array_agg(id) FILTER (WHERE is_main_branch))[1]
                WHEN count(*)=1 THEN (array_agg(id))[1] END AS id
    FROM sub_branches WHERE is_active GROUP BY restaurant_id
)
UPDATE waitlist_entries w SET branch_id=b.id FROM destination b
WHERE w.restaurant_id=b.restaurant_id AND w.branch_id IS NULL AND w.source='qr'
  AND w.status IN ('waiting','notified') AND w.assigned_table_id IS NULL AND b.id IS NOT NULL;
COMMIT;
