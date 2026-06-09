# curl Reference — Route Account Backfill (`/api/v1/super-admin/route`)

Copy-pasteable curl examples for the platform-admin endpoints that seed,
correct, or move a merchant's Razorpay Route linked account
(`rzp_route_accounts`) **without calling Razorpay**. These replace the
ad-hoc `_backfill_*.py` / `_inspect_route_account.py` scripts.

```bash
export API="https://api.bittu.example.com"            # or http://13.206.196.252
export ADMIN_TOKEN="eyJhbGciOi..."                    # platform-admin JWT
export MID="11446751-8e61-4820-be26-e4f4666fd06e"     # merchant (restaurant) id
export ACC="acc_StZVqwekJjxfry"                       # Razorpay linked account id
```

All endpoints require a **platform-admin** JWT (`require_platform_admin`).
The `merchant_id` path param is the Bittu `restaurants.id` (== JWT
`restaurant_id`), NOT the Razorpay contact email.

---

## 1. Inspect current state (before changing anything)

List linked accounts, optionally filtered/searched:

```bash
curl -sS "$API/api/v1/super-admin/route/accounts?search=$ACC&limit=20" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Full detail for one merchant (account + recent transfers + settlement summary):

```bash
curl -sS "$API/api/v1/super-admin/route/accounts/$MID" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Triage queue grouped by onboarding funnel state:

```bash
curl -sS "$API/api/v1/super-admin/route/onboarding-queue?limit=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## 2. Backfill / correct an account (idempotent upsert)

Seed or overwrite a merchant's `rzp_route_accounts` row. Upserts on
`merchant_id` (UNIQUE) so it is safe to re-run. `notes` are **merged**
and always stamped with `bittu_merchant_id`.

### 2.1 Minimal — mark a known account fully activated

```bash
curl -sS -X POST "$API/api/v1/super-admin/route/accounts/$MID/backfill" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "linked_account_id": "'"$ACC"'"
  }'
```

`status`, `kyc_status`, `activation_status`, and `route_product_status`
all default to `"activated"`, so the minimal body activates the account.

### 2.2 Full — seed every field

```bash
curl -sS -X POST "$API/api/v1/super-admin/route/accounts/$MID/backfill" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "linked_account_id":    "'"$ACC"'",
    "status":               "activated",
    "kyc_status":           "activated",
    "activation_status":    "activated",
    "route_product_status": "activated",
    "route_product_id":     "prod_xxxxxxxxxxxx",
    "stakeholder_id":       "sth_xxxxxxxxxxxx",
    "legal_business_name":  "Kings RJ Foods",
    "business_type":        "proprietorship",
    "contact_name":         "RJ",
    "email":                "kingsrj33@gmail.com",
    "phone":                "9876543210",
    "reference_id":         "bittu_'"$MID"'",
    "bank_account_ifsc":    "HDFC0001234",
    "bank_account_last4":   "4321",
    "tnc_accepted":         true,
    "notes": { "reason": "manual backfill — onboarded out-of-band" }
  }'
```

### 2.3 Partial update (only flip product status)

Omitted fields are preserved (`COALESCE` on update). Pass just what you
want to change:

```bash
curl -sS -X POST "$API/api/v1/super-admin/route/accounts/$MID/backfill" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "linked_account_id":    "'"$ACC"'",
    "route_product_status": "activated"
  }'
```

Responses:
- `200` — `{"ok":true,"account":{...}}`
- `400` — invalid `status` (must be one of `created|activated|suspended|rejected|deleted`), or `linked_account_id` already belongs to another merchant (use `/repoint`)
- `404` — merchant (restaurant) not found

---

## 3. Repoint an account to a different merchant

Move an existing `acc_xxx` from its current owner to another merchant.
The target merchant must NOT already own a route account. Stamps
`bittu_merchant_id` and a `repointed_from` audit trail into notes.

```bash
curl -sS -X POST "$API/api/v1/super-admin/route/accounts/$MID/repoint" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "linked_account_id": "'"$ACC"'",
    "notes": { "reason": "wrong owner at onboarding" }
  }'
```

Responses:
- `200` — `{"ok":true,"repointed_from":"<old_merchant>","account":{...}}`
- `400` — already points to this merchant, or target merchant already owns an account
- `404` — `linked_account_id` not found, or target merchant not found

---

## 4. Verify after backfill

```bash
curl -sS "$API/api/v1/super-admin/route/accounts/$MID" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | python -m json.tool
```

Check `status`, `kyc_status`, `route_product_status` are `activated` and
`notes.bittu_merchant_id` equals `$MID`.
