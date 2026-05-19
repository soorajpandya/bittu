#!/usr/bin/env bash
set -e
cd /home/ubuntu/bittu
DBURL=$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'")
psql "$DBURL" -c "SELECT pg_size_pretty(pg_database_size('postgres')) AS db_size, (SELECT count(*) FROM information_schema.tables WHERE table_schema='public') AS tables;"
echo '--- top 10 tables ---'
psql "$DBURL" -c "SELECT schemaname||'.'||relname AS tbl, pg_size_pretty(pg_total_relation_size(relid)) AS size, n_live_tup AS rows FROM pg_catalog.pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
