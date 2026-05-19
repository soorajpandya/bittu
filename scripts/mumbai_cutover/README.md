# Mumbai Cutover Runbook

**Goal:** Move EC2 (Sydney) + Supabase (Seoul) → both in **ap-south-1 (Mumbai)**.
**Expected impact:** Checkout 15–20 s → 1–3 s.
**Downtime:** ~5 min (only during DNS swap + final data sync).
**DB size:** 40 MB / 226 tables / 16 orders — `pg_dump` finishes in seconds.

---

## Prerequisites (you do these in dashboards — no CLI exists)

### 1. Create new Supabase project in Mumbai
1. Go to <https://supabase.com/dashboard/new>
2. Region: **South Asia (Mumbai)** = `ap-south-1`. If the dropdown does not show Mumbai on your plan, pick **Southeast Asia (Singapore)** = `ap-southeast-1`.
3. Save the new project's `DATABASE_URL` (Connection string → **Transaction pooler**, port `6543`). Format:
   `postgresql://postgres.<NEW_REF>:<NEW_PASSWORD>@aws-1-ap-south-1.pooler.supabase.com:6543/postgres`
4. Save the `SUPABASE_URL` and `service_role` key (Project Settings → API).

### 2. Launch new EC2 in Mumbai
- Region: `ap-south-1`
- AMI: **Ubuntu 22.04 LTS**
- Instance type: `t3.small` (same as today; upsize later if needed)
- Security group: inbound 22, 80, 443
- Key pair: reuse `bittu_t3_small_rsa.pem` or create a new one
- Elastic IP: **allocate and attach** (so DNS doesn't need to change again if instance restarts)
- Note the public DNS / Elastic IP

---

## Cutover steps (you run these — copy/paste)

> Replace placeholders:
> - `NEW_EC2_HOST` = Mumbai EC2 public DNS (e.g. `ec2-1-2-3-4.ap-south-1.compute.amazonaws.com`)
> - `NEW_SSH_KEY` = path to the Mumbai SSH key
> - `NEW_DB_URL` = the Mumbai Supabase pooler URL from step 1.3
> - `OLD_DB_URL` = current `DATABASE_URL` from `/home/ubuntu/bittu/.env` on the Sydney box

### Step 1 — Provision the new EC2 (~5 min)

From your Windows machine:

```powershell
# Copy the whole repo + provisioning script to the new box
scp -i $env:USERPROFILE\.ssh\NEW_SSH_KEY -r `
    C:\Users\soora\Downloads\Bittu_Final\backend `
    ubuntu@NEW_EC2_HOST:/home/ubuntu/bittu

# Run provisioner (installs Python, venv, deps, nginx, postgresql-client)
ssh -i $env:USERPROFILE\.ssh\NEW_SSH_KEY ubuntu@NEW_EC2_HOST `
    "bash /home/ubuntu/bittu/scripts/mumbai_cutover/01_provision_ec2.sh"
```

### Step 2 — Replay schema migrations on new Supabase (~30 sec)

From the new Mumbai EC2:
```bash
ssh -i ~/.ssh/NEW_SSH_KEY ubuntu@NEW_EC2_HOST
export NEW_DB_URL='postgresql://postgres....pooler.supabase.com:6543/postgres'
bash /home/ubuntu/bittu/scripts/mumbai_cutover/02_migrate_schema.sh "$NEW_DB_URL"
```

### Step 3 — Dry-run data copy (~30 sec, no downtime)

Validates the dump/restore works end-to-end while old prod is still live.

```bash
export OLD_DB_URL='postgresql://postgres....pooler.supabase.com:6543/postgres'   # Seoul
export NEW_DB_URL='postgresql://postgres....pooler.supabase.com:6543/postgres'   # Mumbai
bash /home/ubuntu/bittu/scripts/mumbai_cutover/03_data_dump_restore.sh "$OLD_DB_URL" "$NEW_DB_URL" --dry-run
```

Expect: `OK: row counts match for N tables`. If mismatched tables are listed, stop and inspect before cutover.

### Step 4 — Write the new `.env` on the Mumbai box

Copy the current `.env` over and edit ONLY `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`:

```powershell
# From your Windows box
scp -i $env:USERPROFILE\.ssh\bittu_t3_small_rsa.pem `
    ubuntu@ec2-3-104-65-176.ap-southeast-2.compute.amazonaws.com:/home/ubuntu/bittu/.env `
    $env:TEMP\bittu.env
# Edit $env:TEMP\bittu.env, replace DATABASE_URL/SUPABASE_URL with Mumbai values
notepad $env:TEMP\bittu.env
scp -i $env:USERPROFILE\.ssh\NEW_SSH_KEY $env:TEMP\bittu.env `
    ubuntu@NEW_EC2_HOST:/home/ubuntu/bittu/.env
Remove-Item $env:TEMP\bittu.env   # don't leave secrets on Windows
```

### Step 5 — Install systemd unit + smoke test (no traffic yet)

```bash
ssh -i ~/.ssh/NEW_SSH_KEY ubuntu@NEW_EC2_HOST
sudo cp /home/ubuntu/bittu/scripts/mumbai_cutover/bittu.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bittu.service
sudo systemctl start bittu.service
sleep 4
systemctl is-active bittu.service                              # → active
curl -sS -w 'health=%{http_code}\n' http://127.0.0.1:8000/api/v1/health   # → 200
```

If health=200, the new box is functional against the new (empty-ish) Mumbai DB.

### Step 6 — Cutover window (~5 min downtime)

Picks the lowest-traffic moment.

**6a. Freeze writes on old prod**
```bash
ssh ubuntu@ec2-3-104-65-176.ap-southeast-2.compute.amazonaws.com "sudo systemctl stop bittu.service"
```

**6b. Final data sync (Seoul → Mumbai)**
```bash
ssh -i ~/.ssh/NEW_SSH_KEY ubuntu@NEW_EC2_HOST
bash /home/ubuntu/bittu/scripts/mumbai_cutover/03_data_dump_restore.sh "$OLD_DB_URL" "$NEW_DB_URL"
```

**6c. Swap DNS**

In your DNS provider (Cloudflare / Route53 / whoever holds `bittupos.com` + `merabittu.com`):
- Update A records for `api.bittupos.com` and `api.merabittu.com` → **new Mumbai Elastic IP**
- TTL: set to 60 s if not already low

**6d. Get HTTPS certs on the new box** (do this *immediately* after DNS propagates)
```bash
ssh -i ~/.ssh/NEW_SSH_KEY ubuntu@NEW_EC2_HOST
sudo apt-get install -y certbot python3-certbot-nginx
sudo cp /home/ubuntu/bittu/scripts/mumbai_cutover/nginx_bittu.conf /etc/nginx/sites-available/bittu
sudo ln -sf /etc/nginx/sites-available/bittu /etc/nginx/sites-enabled/bittu
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d api.bittupos.com -d api.merabittu.com --non-interactive --agree-tos -m owner@bittupos.com
```

**6e. Validate from public internet**
```bash
curl -sS -w 'health=%{http_code}\n' https://api.bittupos.com/api/v1/health
# Then place ONE real test order from POS to confirm checkout works
```

### Step 7 — Keep Sydney as cold standby for 48 h

Do NOT terminate the Sydney instance yet:
```bash
ssh ubuntu@ec2-3-104-65-176.ap-southeast-2.compute.amazonaws.com "sudo systemctl disable bittu.service"
# Then STOP (not terminate) the instance in AWS console
```

If anything breaks in the first 48 h, revert by:
1. Starting Sydney instance
2. Reverting DNS A records to Sydney Elastic IP
3. Restoring Seoul DB from last `pg_dump` (saved by the cutover script in `/tmp/bittu_dump_*.sql`)

After 48 h of clean operation: terminate Sydney instance.

---

## What you do NOT need to change in Razorpay

The webhook URL (`https://api.bittupos.com/api/v1/webhooks/razorpay`) follows DNS automatically. No Razorpay dashboard change required.

## Measured RTT after cutover

Run this on the Mumbai box to confirm same-region latency:
```bash
python3 /home/ubuntu/bittu/scripts/_rtt.py aws-1-ap-south-1.pooler.supabase.com 6543
# Expect: 1–5 ms (vs 140 ms today)
```
