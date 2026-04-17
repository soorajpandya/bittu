-- Migration 010: Bridge accounting_entries to double-entry journals (additive only)
-- Safe for live systems: no drops, no type changes, no removals.

-- Keep this CREATE as a compatibility guard for environments that do not yet
-- have journal_entries. In environments that already have the richer table,
-- IF NOT EXISTS ensures this is skipped.
CREATE TABLE IF NOT EXISTS journal_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  restaurant_id uuid,
  reference_type text,
  reference_id text,
  description text,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE accounting_entries
ADD COLUMN IF NOT EXISTS journal_entry_id uuid REFERENCES journal_entries(id);

ALTER TABLE accounting_entries
ADD COLUMN IF NOT EXISTS entry_side text CHECK (entry_side IN ('debit','credit'));

CREATE INDEX IF NOT EXISTS idx_accounting_entries_journal_entry_id
  ON accounting_entries(journal_entry_id)
  WHERE journal_entry_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_accounting_entries_entry_side
  ON accounting_entries(entry_side)
  WHERE entry_side IS NOT NULL;
