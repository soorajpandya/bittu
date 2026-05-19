#!/usr/bin/env bash
# Dump data from OLD Supabase, restore into NEW Supabase.
# Schema-only objects (extensions, types) are EXCLUDED — those came from
# replaying /migrations via 02_migrate_schema.sh. We only ship data here.
#
# Usage:
#   03_data_dump_restore.sh "$OLD_DB_URL" "$NEW_DB_URL"             # real run
#   03_data_dump_restore.sh "$OLD_DB_URL" "$NEW_DB_URL" --dry-run   # validates only
set -euo pipefail

OLD="${1:?usage: $0 <OLD_DB_URL> <NEW_DB_URL> [--dry-run]}"
NEW="${2:?usage: $0 <OLD_DB_URL> <NEW_DB_URL> [--dry-run]}"
DRY="${3:-}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP=/tmp/bittu_data_${STAMP}.sql

echo "==> dumping data from OLD → $DUMP"
# --data-only:     skip schema (already created via migrations)
# --disable-triggers: speed + avoid FK ordering issues
# --no-owner:      Supabase role isn't superuser of objects
# --no-privileges: skip GRANT/REVOKE (RLS comes from migrations)
# Schemas of interest: public + auth (Supabase auth.users etc).
pg_dump "$OLD" \
    --data-only \
    --disable-triggers \
    --no-owner \
    --no-privileges \
    --schema=public \
    --schema=auth \
    --schema=storage \
    --file "$DUMP"

DUMP_BYTES=$(stat -c%s "$DUMP" 2>/dev/null || stat -f%z "$DUMP")
echo "==> dump size: ${DUMP_BYTES} bytes"

if [ "$DRY" = "--dry-run" ]; then
    echo
    echo "==> [dry-run] restoring into NEW into a temporary savepoint for validation"
    psql "$NEW" --single-transaction --set ON_ERROR_STOP=1 -c "BEGIN; SAVEPOINT pre_restore;" \
        -f "$DUMP" -c "ROLLBACK TO SAVEPOINT pre_restore; ROLLBACK;" \
        >/tmp/_restore.log 2>&1 || { echo "DRY-RUN FAILED"; tail -50 /tmp/_restore.log; exit 1; }
    echo "==> [dry-run] OK — restore would succeed"
else
    echo
    echo "==> RESTORING into NEW (this overwrites! kill in next 5s if not ready)"
    sleep 5
    psql "$NEW" --set ON_ERROR_STOP=1 -f "$DUMP" >/tmp/_restore.log 2>&1 || {
        echo "RESTORE FAILED — see /tmp/_restore.log"
        tail -50 /tmp/_restore.log
        exit 1
    }
    echo "==> restore complete"
fi

echo
echo "==> row-count comparison (top critical tables)"
for tbl in orders payments order_items users restaurants payment_intents \
          merchant_ledger escrow_ledger checkout_idempotency; do
    OLD_C=$(psql "$OLD" -At -c "SELECT count(*) FROM ${tbl}" 2>/dev/null || echo "n/a")
    NEW_C=$(psql "$NEW" -At -c "SELECT count(*) FROM ${tbl}" 2>/dev/null || echo "n/a")
    if [ "$OLD_C" = "$NEW_C" ]; then
        printf '  %-25s  old=%-8s new=%-8s  MATCH\n' "$tbl" "$OLD_C" "$NEW_C"
    else
        printf '  %-25s  old=%-8s new=%-8s  *** MISMATCH ***\n' "$tbl" "$OLD_C" "$NEW_C"
    fi
done

echo
echo "Dump preserved at $DUMP for rollback."
