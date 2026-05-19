#!/usr/bin/env bash
# Replay every numbered migration in /migrations against a fresh Supabase DB.
# Usage: 02_migrate_schema.sh "$NEW_DB_URL"
set -euo pipefail

DB_URL="${1:?usage: $0 <NEW_DB_URL>}"
MIG_DIR="$(cd "$(dirname "$0")/../../migrations" && pwd)"

echo "==> migrations source: $MIG_DIR"
echo "==> target: ${DB_URL%%@*}@*** (host hidden)"
echo

# Order strictly by filename prefix (NNN_*.sql). Skip non-sql / non-numeric.
shopt -s nullglob
files=( "$MIG_DIR"/[0-9][0-9][0-9]_*.sql )
echo "==> ${#files[@]} migration files queued"

count=0
for f in "${files[@]}"; do
    bn="$(basename "$f")"
    count=$((count + 1))
    printf '[%3d/%3d] %s ... ' "$count" "${#files[@]}" "$bn"
    # ON_ERROR_STOP makes psql exit non-zero on first SQL error so the loop aborts.
    if psql "$DB_URL" --quiet --no-align --tuples-only \
            --set ON_ERROR_STOP=1 -f "$f" >/tmp/_mig_out.$$ 2>&1; then
        echo "OK"
    else
        echo "FAIL"
        echo "----- $bn output -----"
        cat /tmp/_mig_out.$$
        rm -f /tmp/_mig_out.$$
        exit 1
    fi
    rm -f /tmp/_mig_out.$$
done

echo
echo "==> schema sanity check"
psql "$DB_URL" -c "SELECT count(*) AS public_tables FROM information_schema.tables WHERE table_schema='public';"
psql "$DB_URL" -c "SELECT count(*) AS enums FROM pg_type WHERE typtype='e';"

echo
echo "Done. Next step: 03_data_dump_restore.sh \"\$OLD_DB_URL\" \"\$NEW_DB_URL\" --dry-run"
