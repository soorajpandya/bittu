# curl Reference — `/api/v1/bankkyc_razorpay`

Companion to `FRONTEND_PROMPT_bankkyc_razorpay.md`. Every endpoint
below has a copy-pasteable curl example against prod.

```bash
export API="https://api.bittu.example.com"            # or http://13.206.196.252
export TOKEN="eyJhbGciOi..."                          # merchant JWT
export ADMIN_TOKEN="eyJhbGciOi..."                    # platform-admin JWT
```

---

## 1. Merchant endpoints

### 1.1 Submit KYC

```bash
curl -sS -X POST "$API/api/v1/bankkyc_razorpay" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "account_name":      "Burptech Private Limited",
    "account_email":     "owner@burptech.in",
    "business_name":     "Burptech Private Limited",
    "business_type":     "private_limited",
    "ifsc_code":         "IDFB0040313",
    "account_number":    "10257141036",
    "beneficiary_name":  "Burptech Private Limited",
    "dashboard_access":  0,
    "customer_refunds":  0
  }'
```

Responses:
- `200` — `{"success":true,"submission_id":1,"status":"PENDING_BATCH_UPLOAD",...}`
- `409` — already has live submission (fetch `/status`)
- `422` — field validation errors

### 1.2 Get my submission status

```bash
curl -sS "$API/api/v1/bankkyc_razorpay/status" \
  -H "Authorization: Bearer $TOKEN"
```

---

## 2. Admin endpoints

All require `Authorization: Bearer $ADMIN_TOKEN` (platform-admin role).

### 2.1 Stats / KPI tiles

```bash
curl -sS "$API/api/v1/bankkyc_razorpay/admin/stats" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### 2.2 List batches

```bash
curl -sS "$API/api/v1/bankkyc_razorpay/admin/batches?limit=50&offset=0" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### 2.3 List submissions (optionally filter by batch / status)

```bash
# all
curl -sS "$API/api/v1/bankkyc_razorpay/admin/submissions?limit=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# by batch
curl -sS "$API/api/v1/bankkyc_razorpay/admin/submissions?batch_id=12&limit=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# by status
curl -sS "$API/api/v1/bankkyc_razorpay/admin/submissions?status=PENDING_BATCH_UPLOAD" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### 2.4 Force-generate current 30-min slot

```bash
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/generate" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### 2.5 Download batch file (the one to upload to Razorpay Dashboard)

```bash
# CSV
curl -sS -OJ "$API/api/v1/bankkyc_razorpay/admin/batches/12/csv" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# XLSX (recommended — matches Razorpay's Test_Batch_Upload.xlsx)
curl -sS -OJ "$API/api/v1/bankkyc_razorpay/admin/batches/12/xlsx" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

`-OJ` honors the server's `Content-Disposition` filename
(e.g. `BATCH-20260526-1000.xlsx`). Upload **that file** to
Razorpay Dashboard → Batch Uploads → Linked Account Creation.

### 2.6 Mark whole batch — uploaded / approved / rejected

```bash
# After you've uploaded the file to Razorpay Dashboard:
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/12/mark-uploaded" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# After Razorpay accepts all rows (optionally pass acc_xxx mapping):
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/12/mark-approved" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "razorpay_account_ids": {
      "1": "acc_StZVqwekJjxfry"
    }
  }'

# Or, mark approved without ids:
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/12/mark-approved" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" -d '{}'

# Reject entire batch with a reason:
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/12/mark-rejected" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"Razorpay returned: invalid IFSC for 3 rows"}'
```

### 2.7 Per-submission actions

```bash
# Approve one (acc id optional)
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/submissions/1/mark-approved" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"razorpay_account_id":"acc_StZVqwekJjxfry"}'

# Reject one
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/submissions/1/mark-rejected" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason":"Bank account name mismatch with PAN"}'

# Reconcile from Razorpay (calls GET /v2/accounts/{acc_xxx} server-side)
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/submissions/1/check-account" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## 3. End-to-end happy path (one merchant)

```bash
# 1) Merchant submits
curl -sS -X POST "$API/api/v1/bankkyc_razorpay" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @submission.json

# 2) Admin waits for next 30-min slot OR force-generates now
BATCH_ID=$(curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/generate" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.id')

# 3) Download the file Razorpay expects
curl -sS -OJ "$API/api/v1/bankkyc_razorpay/admin/batches/$BATCH_ID/xlsx" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# 4) Upload that .xlsx in Razorpay Dashboard → Batch Uploads, then:
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/$BATCH_ID/mark-uploaded" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# 5) Once Razorpay accepts, mark approved with the acc_xxx ids:
curl -sS -X POST "$API/api/v1/bankkyc_razorpay/admin/batches/$BATCH_ID/mark-approved" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"razorpay_account_ids":{"1":"acc_xxx"}}'
```
