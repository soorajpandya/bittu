#!/usr/bin/env bash
# Provision a fresh Ubuntu 22.04 EC2 to run Bittu.
# Idempotent: safe to re-run.
set -euo pipefail

REPO=/home/ubuntu/bittu

echo "==> apt update + system packages"
sudo apt-get update -y
sudo apt-get install -y \
    python3 python3-venv python3-dev python3-pip \
    build-essential libpq-dev \
    postgresql-client-14 \
    nginx \
    git curl ca-certificates

echo "==> create venv"
if [ ! -d "$REPO/venv" ]; then
    python3 -m venv "$REPO/venv"
fi
"$REPO/venv/bin/pip" install --upgrade pip wheel
"$REPO/venv/bin/pip" install -r "$REPO/requirements.txt"

echo "==> verify import"
cd "$REPO"
"$REPO/venv/bin/python" -c "import app; print('app import OK')"

echo "==> enable nginx"
sudo systemctl enable nginx
sudo systemctl start nginx

echo "==> open firewall (ufw is usually off on EC2 — relying on SG)"
echo "    Ensure EC2 security group allows 22, 80, 443 inbound."

echo
echo "Done. Next step: 02_migrate_schema.sh \"\$NEW_DB_URL\""
