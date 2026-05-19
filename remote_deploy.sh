cd /home/ubuntu/bittu
git fetch --all
git reset --hard origin/main
DB_URL=$(grep -E '^DATABASE_DIRECT_URL=' .env | head -1 | sed 's/^DATABASE_DIRECT_URL=//; s/^"//; s/"$//')
echo "Have DB_URL length: ${#DB_URL}"
psql "$DB_URL" -v ON_ERROR_STOP=1 -f migrations/063_geofence_and_saas_invoices.sql
sudo systemctl restart bittu.service
sleep 5
systemctl is-active bittu.service
(curl -fsS http://127.0.0.1:8000/api/v1/health || curl -fsS http://127.0.0.1:8000/health)
journalctl -u bittu.service -n 20 --no-pager
