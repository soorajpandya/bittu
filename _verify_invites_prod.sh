#!/usr/bin/env bash
set -euo pipefail
cd ~/bittu
source venv/bin/activate

TOKEN=$(python - <<'PY'
import jwt
from app.core.config import get_settings
s = get_settings()
payload = {
  'sub': 'a07da9d2-1235-4af5-bcd3-9ba56b6edc47',
  'email': 'owner@test.local',
  'aud': 'authenticated'
}
print(jwt.encode(payload, s.SUPABASE_JWT_SECRET, algorithm='HS256'))
PY
)

CODE1=$(curl -s -o /tmp/perm.json -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8000/api/v1/auth/permissions/me)
CODE2=$(curl -s -o /tmp/inv.json -w '%{http_code}' -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8000/api/v1/staff/invites)

echo "permissions_me_status=${CODE1}"
echo "staff_invites_status=${CODE2}"

python - <<'PY'
import json

p = json.load(open('/tmp/perm.json', 'r', encoding='utf-8'))
perms = p.get('permissions', {}) if isinstance(p, dict) else {}
print('perm_staff_invites_read', perms.get('staff.invites.read'))
print('perm_staff_invites_create', perms.get('staff.invites.create'))
print('perm_staff_invites_revoke', perms.get('staff.invites.revoke'))

inv = json.load(open('/tmp/inv.json', 'r', encoding='utf-8'))
if isinstance(inv, list):
    print('staff_invites_count', len(inv))
else:
    print('staff_invites_payload_type', type(inv).__name__)
    if isinstance(inv, dict):
        print('staff_invites_detail', inv.get('detail'))
PY
