#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/bittu
DATABASE_URL=$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)
export DATABASE_URL
psql "$DATABASE_URL" <<'SQL'
\echo === payments by (restaurant, status, method) ===
SELECT restaurant_id, status, method, COUNT(*) AS cnt, SUM(amount)::numeric(14,2) AS total
FROM payments GROUP BY 1,2,3 ORDER BY cnt DESC LIMIT 30;

\echo === bittu_settlements by (restaurant, settlement_status) ===
SELECT restaurant_id, settlement_status, COUNT(*) AS cnt, SUM(gross_amount)::numeric(14,2) AS gross
FROM bittu_settlements GROUP BY 1,2 ORDER BY cnt DESC LIMIT 30;

\echo === distinct methods ===
SELECT DISTINCT method FROM payments;

\echo === distinct statuses ===
SELECT DISTINCT status FROM payments;
SQL
