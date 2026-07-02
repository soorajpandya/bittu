# Frontend Guide — Complete the Onboarding Flow (backend is LIVE)

Everything the frontend needs to finish onboarding end-to-end, against
`https://api.bittupos.com`. All endpoints are JWT-gated under `/api/v1`
(send `Authorization: Bearer <token>`). Statuses, request/response shapes
and field names below are the exact backend contract.

Wizard = **3 steps**:
1. Choose plan  → (subscription gate for starter/business)
2. Restaurant settings (profile + branding + operations + GST)
3. KYC & payouts  → POS

---

## 0. Routing brain — read this on every session restore

After login, call **`GET /onboarding/state`** and route off it:

```jsonc
{
  "plan": "growth",                 // string | null
  "plan_selected": true,            // bool
  "subscription_paid": true,        // bool  (true when plan needs no sub)
  "kyc_submitted": false,           // bool  (derived from KYC status)
  "onboarding_complete": false,     // bool  (plan + sub + kyc)
  "can_proceed_to_settings": true,  // bool  (plan + subscription_paid)

  "requires_subscription": false,   // starter/business => true
  "requires_device": false,         // business/enterprise => true
  "device_paid": false,
  "kyc_status": "NOT_SUBMITTED",    // see §4

  "subscription": { "required": false, "status": null },
  "device":       { "required": false, "status": null },
  "plan_meta":    { /* same per-plan object as /onboarding/plans */ },
  "steps": { "plan_selected": true, "subscription_paid": true, "kyc_submitted": false }
}
```

Routing rules:
- `onboarding_complete === true`  → go to `/pos`.
- else `can_proceed_to_settings === true` → Step 2 (Settings) / Step 3 (KYC).
- else → Step 1 (plan + subscription gate).

Graceful-degrade: if the call 404s (older deploy) treat state as null and
proceed un-gated (legacy). The endpoints are live now, so this won't happen.

---

## STEP 1 — Choose plan + subscription gate

### 1.1 `GET /onboarding/plans` (server-authoritative catalog)
```jsonc
{
  "prices_exclude_gst": true,
  "currency": "INR",
  "default_plan": "growth",
  "gst_percent": 18.0,
  "plans": [
    { "id": "starter",    "name": "Software",            "tagline": "For restaurants using their own UPI.",
      "subscription": { "amount": 5000, "interval": "yearly" }, "device_fee": null,
      "transaction_fee_percent": null, "reconciliation": "manual",    "payments": "own_upi",    "payment_gated": true },
    { "id": "business",   "name": "Software + Device",    "tagline": "Dedicated Bittu POS hardware.",
      "subscription": { "amount": 5000, "interval": "yearly" }, "device_fee": { "amount": 30000, "billing": "separate_one_time" },
      "transaction_fee_percent": null, "reconciliation": "manual",    "payments": "own_upi",    "payment_gated": true },
    { "id": "growth",     "name": "Integrated Payments",  "tagline": "Automatic reconciliation included.",
      "subscription": { "amount": 0, "interval": null },       "device_fee": null,
      "transaction_fee_percent": 1.75, "reconciliation": "automatic", "payments": "integrated", "payment_gated": false },
    { "id": "enterprise", "name": "Complete Suite",       "tagline": "Hardware + payments in one platform.",
      "subscription": { "amount": 0, "interval": null },       "device_fee": { "amount": 30000, "billing": "separate_one_time" },
      "transaction_fee_percent": 1.75, "reconciliation": "automatic", "payments": "integrated", "payment_gated": false }
  ]
}
```
IDs are exactly `starter | business | growth | enterprise`. Default = `growth`.

### 1.2 `PUT /onboarding/plan` (persist choice — call BEFORE subscription/device)
- Request: `{ "plan": "starter|business|growth|enterprise" }`
- Returns the refreshed onboarding state.

### 1.3 `POST /onboarding/subscription` (create/reuse the gate)
- `growth` / `enterprise` → `{ "required": false, "plan": "...", "reason": "..." }`
  → no payment, go straight to Step 2.
- `starter` / `business` →
```json
{
  "required": true,
  "razorpay_subscription_id": "sub_XXXX",
  "key_id": "rzp_xxx",
  "short_url": "https://rzp.io/rzp/xxxx",
  "status": "created"
}
```
Idempotent — calling twice reuses the same subscription.

Open Razorpay Checkout (subscription mode):
```js
const r = await api.post("/onboarding/subscription");
if (r.required) {
  new Razorpay({
    key: r.key_id,
    subscription_id: r.razorpay_subscription_id,
    name: "Bittu POS",
    description: "Annual subscription",
    handler: async (resp) => {
      await api.post("/onboarding/subscription/verify", {
        razorpay_payment_id:      resp.razorpay_payment_id,
        razorpay_subscription_id: resp.razorpay_subscription_id,
        razorpay_signature:       resp.razorpay_signature,
      });
      await refetchOnboardingState(); // subscription_paid -> true
    },
  }).open();
} else {
  goToSettings();
}
```

### 1.4 `POST /onboarding/subscription/verify`
- Request: `{ razorpay_payment_id, razorpay_subscription_id, razorpay_signature }`
- Returns refreshed state. After success → `can_proceed_to_settings === true`.

### 1.5 `GET /onboarding/subscription` (read current subscription status)
Returns the `subscription` object from state. `status` is one of
`created | authenticated | active | pending | halted | cancelled | completed`;
treat `authenticated`/`active` as paid.

---

## STEP 2 — Restaurant settings (two endpoints)

First get the restaurant id: **`GET /restaurants`** → array; use `[0].id`.

### 2.1 Profile + branding → `PATCH /restaurants/{restaurant_id}`
Partial update; send only changed fields. Accepted fields:
```
name, phone, email, address, city, state, pincode,
latitude, longitude, logo_url, cover_url,
gst_number, fssai_number, is_active,
opening_time ("HH:MM"), closing_time ("HH:MM"), avg_prep_time (int),
plan   // also persists the onboarding plan if you prefer this over PUT /onboarding/plan
```
Logo/cover: upload to your Supabase storage, then PATCH the public `logo_url`.

### 2.2 Operations + GST/tax → `PUT /restaurant-settings`
Requires the `settings.admin` permission. Accepted fields:
```
// operations
enable_dine_in, enable_takeaway, enable_delivery, enable_qr_ordering (bool)
auto_accept_orders (bool)
// GST / tax
gst_enabled (bool)
gst_type (string, e.g. "regular"|"composition")
gst_number (string)
gst_percentage, cgst_percentage, sgst_percentage (0..28)
tax_inclusive (bool)        // <-- this is your "prices_include_gst"
tax_percentage (0..28)
currency, receipt_header, receipt_footer
printer_config, theme_config (objects), LED/dual-screen fields
```
> Field mapping note: your `prices_include_gst` boolean maps to backend
> **`tax_inclusive`**. There is no separate `prices_include_gst` column.

`GET /restaurant-settings` returns the current settings (needs `settings.read`).

---

## STEP 3 — KYC & payouts (this flips `onboarding_complete`)

Two parts. Part A is required for ALL plans to complete onboarding; Part B is
the integrated-payments provisioning for `growth`/`enterprise`.

### 3.A Submit bank + KYC → `POST /bankkyc_razorpay`
Requires the `razorpay.route.write` permission. This is what creates the KYC
submission so `kyc_status` leaves `NOT_SUBMITTED` and `onboarding_complete`
becomes true.
- Request (`extra="forbid"` — send exactly these keys):
```json
{
  "account_name": "Owner Name",
  "account_email": "owner@example.com",
  "business_name": "My Restaurant",
  "business_type": "proprietorship",
  "ifsc_code": "HDFC0001234",
  "account_number": "1234567890",
  "beneficiary_name": "My Restaurant",
  "dashboard_access": 0,
  "customer_refunds": 0,
  "notes": null
}
```
- Response:
```json
{
  "success": true,
  "message": "Your details have been submitted successfully. Bittu POS will update your KYC status within 4 to 8 hours.",
  "submission_id": 123,
  "status": "PENDING_BATCH_UPLOAD",
  "estimated_processing_window": "4-8 hours",
  "next_batch_slot_utc": "2026-06-20T10:30:00+00:00"
}
```
After this returns, re-fetch `GET /onboarding/state` → `kyc_submitted: true`,
`onboarding_complete: true` (given plan + subscription_paid) → route to `/pos`.

### 3.B (growth/enterprise only) Provision Razorpay Route linked account
Needed so integrated-payment merchants actually receive auto-split
settlements. Single round-trip: `POST /razorpay-route/linked-account/onboard`
(requires `razorpay.route.write`). Body (bank required; KYC profile optional —
if supplied it seeds the merchant_kyc_* tables in the same call):
```json
{
  "bank_account_number": "1234567890",
  "ifsc": "HDFC0001234",
  "beneficiary_name": "My Restaurant",
  "tnc_accepted": true,
  "legal_name": "My Restaurant Pvt Ltd",
  "business_type": "proprietorship",
  "pan": "ABCDE1234F",
  "gstin": "27ABCDE1234F1Z5",
  "contact_email": "owner@example.com",
  "contact_phone": "9876543210",
  "owner_name": "Owner Name",
  "owner_role": "proprietor",
  "category": "food",
  "subcategory": "restaurant"
}
```
Status reads: `GET /razorpay-route/linked-account` (light) and
`GET /razorpay-route/linked-account/details` (full Razorpay payload).

### 3.C KYC status (for a status screen)
`GET /bankkyc_razorpay/status` (or read `kyc_status` from onboarding state):
- `NOT_SUBMITTED` → show the KYC form.
- `PENDING_BATCH_UPLOAD` / `IN_BATCH_FILE` / `UPLOADED*` → "under review, 4–8h".
- `APPROVED` → done. `REJECTED` → show reason, allow resubmit.

---

## STEP 0.5 — Sub-merchant agreement (audit trail, optional gate)

If you display the agreement, record acceptance:
`POST /merchant/agreement-acceptance` (requires merchant role):
```json
{
  "version": "v1.0",
  "agreement_sha256": "<64-hex sha256 of the exact agreement text shown>",
  "accepted_at_client": "2026-06-20T10:15:00.000Z",
  "user_agent": "<navigator.userAgent>",
  "ip_client": "<optional>"
}
```
- `GET /merchant/agreement-acceptance/latest` → most recent acceptance.
- `GET /merchant/agreement-acceptance?limit=50&offset=0` → history.
The server stamps the authoritative IP/timestamp; client values are stored
separately for audit. Server returns `201` with the stored record.

---

## Device fee (business + enterprise) — separate one-time charge

Does NOT gate Settings/KYC. ₹30,000 + GST, its own Razorpay **order**. Applies
to `business` AND `enterprise` (yes, even though enterprise has a ₹0 sub).

- `POST /onboarding/device-fee` → for device plans:
```json
{ "required": true, "razorpay_order_id": "order_xxx", "amount": 30000,
  "gst": 5400.0, "gst_percent": 18.0, "total": 35400.0, "currency": "INR",
  "key_id": "rzp_xxx", "billing": "separate_one_time", "status": "created" }
```
  (non-device plans → `{ "required": false, ... }`)
- `POST /onboarding/device-fee/verify` → `{ razorpay_payment_id, razorpay_order_id, razorpay_signature }`
- `GET /onboarding/device-fee` → status (`device_paid` also on state).

Checkout (order mode, not subscription):
```js
const d = await api.post("/onboarding/device-fee");
if (d.required && d.status !== "paid") {
  new Razorpay({
    key: d.key_id, order_id: d.razorpay_order_id,
    amount: Math.round(d.total * 100), currency: "INR",
    name: "Bittu POS", description: "POS device (one-time)",
    handler: async (resp) => {
      await api.post("/onboarding/device-fee/verify", {
        razorpay_payment_id: resp.razorpay_payment_id,
        razorpay_order_id:   resp.razorpay_order_id,
        razorpay_signature:  resp.razorpay_signature,
      });
      await refetchOnboardingState();
    },
  }).open();
}
```
You may let the merchant pay the device fee later — it doesn't block POS.

---

## Pricing to DISPLAY (backend enforces the money)

| Plan | Subscription | Device (separate) | Per-txn fee | Gated? |
|---|---|---|---|---|
| starter | ₹5,000/yr + GST | — | — | Yes |
| business | ₹5,000/yr + GST | ₹30,000 + GST | — | Yes |
| growth | ₹0 | — | 1.75% + GST | No |
| enterprise | ₹0 | ₹30,000 + GST | 1.75% + GST | No |

- Integrated-payments fee (growth/enterprise): **1.75% + 18% GST** deducted per
  transaction; merchant nets **97.935%**. Backend handles the split at
  settlement — just display "1.75% + GST".
- All catalog prices are GST-exclusive; add `gst_percent` for display totals.

---

## End-to-end happy path (pseudocode)

```
state = GET /onboarding/state
if (state.onboarding_complete) -> /pos

// Step 1
plans = GET /onboarding/plans
PUT /onboarding/plan { plan }
sub = POST /onboarding/subscription
if (sub.required) -> Razorpay(subscription) -> POST /onboarding/subscription/verify
state = GET /onboarding/state            // expect can_proceed_to_settings == true
if (plan in [business, enterprise]) optionally POST /onboarding/device-fee -> verify

// Step 2
rid = (GET /restaurants)[0].id
PATCH /restaurants/{rid} { profile... }
PUT   /restaurant-settings { gst + operations... }

// Step 3
POST /bankkyc_razorpay { bank + kyc }    // flips kyc_submitted
if (plan in [growth, enterprise]) POST /razorpay-route/linked-account/onboard { bank + kyc }
state = GET /onboarding/state            // expect onboarding_complete == true
-> /pos
```

---

## Permissions / gotchas

- KYC endpoints need `razorpay.route.write` / `razorpay.route.read`; settings
  need `settings.admin` / `settings.read`; FSSAI verify needs `kyc:verify`.
  Make sure the merchant owner role carries these (it should by default).
- `_mid` for KYC/settings requires an active `restaurant_id` in the session —
  create the restaurant first (`POST /restaurants` or it's auto-initialised at
  signup) so the JWT/session resolves a restaurant.
- Always `PUT /onboarding/plan` BEFORE `POST /onboarding/subscription` or
  `/device-fee` (both 400 if no plan is set).
- Re-fetch `GET /onboarding/state` after every verify/submit and route off
  `can_proceed_to_settings` / `onboarding_complete`.
- `onboarding_complete` stays derived from KYC status, so returning merchants
  route to `/pos` correctly on re-login.
- FSSAI license check (optional, in Step 2): `POST /kyc/verify/fssai`
  `{ "reg": "<14-digit>", "fetch_products": false }`.
```
