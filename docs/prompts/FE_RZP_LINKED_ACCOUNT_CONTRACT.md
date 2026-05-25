# Frontend prompt — Razorpay Linked Account onboarding (v2/accounts spec-aligned)

> Deployed: `82fc282` on `ec2-13-206-196-252` (Mumbai), `/api/v1/health → 200 ok`.
> Backend is now strictly aligned with Razorpay's `POST /v2/accounts` spec.
> Use this contract when wiring the Bittu app's Razorpay onboarding flow.

---

## 1. Endpoints (unchanged paths, tighter contracts)

Base URL: `https://<host>/api/v1/razorpay-route`

| Method | Path | Purpose |
|---|---|---|
| `GET`   | `/linked-account` | Current local row (status, gateway id, KYC mirror). Cheap, no gateway call. |
| `GET`   | `/linked-account/details` | **Full Razorpay payload** (mirrors `GET /v2/accounts/:id`). Re-syncs local row. |
| `POST`  | `/linked-account/provision` | Idempotent — create on Razorpay if missing, else resync. |
| `PATCH` | `/linked-account` | Merchant-driven update (mirrors Razorpay `PATCH /v2/accounts/:id`). |
| `POST`  | `/linked-account/sync` | Pull latest state from Razorpay → local row. |
| `POST`  | `/linked-account/stakeholder` | Create / refresh stakeholder. |
| `POST` | `/linked-account/product` | Request `route` product. |
| `PATCH`| `/linked-account/product` | Submit settlement bank details. |
| `POST` | `/linked-account/product/sync` | Refresh product (`activated` etc.). |
| `POST` | `/linked-account/onboard` | **One-shot end-to-end** (provision → stakeholder → product → bank). |

All require permission `razorpay.route.read` (GET) or `razorpay.route.write` (POST/PATCH).
Standard auth header: `Authorization: Bearer <jwt>`.

---

## 2. `POST /linked-account/provision` — request body

Every field is **optional**. Send only what you have; the backend pulls the rest from the merchant's KYC profile.

```jsonc
{
  "bank_account_number": "string",            // in-memory only, stored as last4+sha256
  "ifsc": "HDFC0001234",
  "beneficiary_name": "Acme Foods Pvt Ltd",   // 1..255, alphabets/digits/spaces only after sanitisation

  "reference_id": "m_ab12cd34ef",             // 3..20 chars of [A-Za-z0-9_-]; auto-dropped if platform lacks the route_code_support feature
  "notes": { "campaign": "diwali_2026" },

  // Razorpay profile.* fields
  "category": "food",                          // default "food"
  "subcategory": "restaurant",                 // default "restaurant"
  "addresses": {
    "registered": {
      "street1": "12 MG Road",
      "street2": "Indiranagar",
      "city": "Bengaluru",
      "state": "KA",                           // full name OR 2-letter code; both accepted
      "postal_code": "560038",
      "country": "IN"
    },
    "operation": { /* same shape, optional */ }
  },

  // Razorpay top-level optional pass-throughs (NEW)
  "customer_facing_business_name": "Acme Diner",   // 1..255
  "contact_info": {
    "chargeback": { "email": "ops@acme.com", "phone": "9876543210" },
    "refund":     { "email": "refunds@acme.com", "phone": "9876543210" },
    "support":    { "email": "help@acme.com",   "phone": "9876543210", "policy_url": "https://acme.com/policy" }
  },
  "apps": { "websites": ["https://acme.com"] }
}
```

### Field rules the backend enforces (so the FE doesn't need to re-do them)

| Field | Rule |
|---|---|
| `reference_id` | Pydantic `min_length=3, max_length=20, pattern=^[A-Za-z0-9_-]+$`. If the platform account lacks `route_code_support`, backend retries once *without* the field and logs an audit row — FE always sees a 200 either way. |
| `addresses.registered.state` | Accepted as `"Karnataka"`, `"KARNATAKA"`, `"karnataka"` or `"KA"`. Backend normalises to the Razorpay 2-letter code. |
| `addresses.registered.country` | `"India"`, `"INDIA"`, `"in"` and `"IN"` all map to `"IN"`. |
| `addresses.registered.postal_code` | Non-digits stripped server-side. Must end up non-empty. |
| `beneficiary_name` (and any `contact_name`) | Characters outside `[A-Za-z0-9 ]` are stripped server-side; rejected if the cleaned name is shorter than 4 chars. |
| `customer_facing_business_name` | 1..255 chars. Defaults to `legal_business_name` on Razorpay's side if omitted. |
| `pan` / `gstin` (read from KYC) | Regex-validated server-side; an invalid value is **silently dropped** (logged), the account is still created. |
| Contact `phone` | Digits-only; leading `91` is stripped if the remainder is 8..15 digits. |

### Response (200)

Returns the local `rzp_route_accounts` row (or the freshly-created/adopted one):

```json
{
  "merchant_id": "751c6d1d-…",
  "linked_account_id": "acc_SruqmpCeoXIesu",
  "status": "created",                  // local enum: created | activated | suspended | rejected | deleted
  "razorpay_status": "under_review",    // raw gateway status, may be needs_clarification etc.
  "route_product_id": "acc_prd_StZ8yQRr7W8xDM",
  "route_product_status": "activated",  // requested | under_review | needs_clarification | activated | rejected
  "effective_status": "activated",      // ⭐ SINGLE SOURCE OF TRUTH for FE UI branching — see below
  "reference_id": "m_751c6d1d15594…",
  "bank_account_last4": "1234",
  "bank_ifsc": "HDFC0001234",
  "created_at": "2026-05-25T09:42:25Z",
  "updated_at": "2026-05-25T09:42:25Z"
}
```

`bank_account_hash` is **never** exposed.

### `effective_status` — the only field the FE should branch on

Razorpay splits onboarding state across two independent fields (`status` on the account, `activation_status` on the route product). To avoid every client re-implementing that composition (and getting it wrong — accounts stay `status: created` forever even after activation, the green badge actually comes from `route_product_status: activated`), the backend now returns a derived `effective_status`:

| `effective_status`       | When                                                              | FE should show                                              |
| ------------------------ | ----------------------------------------------------------------- | ----------------------------------------------------------- |
| `pending`                | `linked_account_id == null`                                       | Onboarding form (collect KYC, call `POST /provision`).      |
| `submitted`              | Account exists, no `route_product_id` yet                         | "Submitted — finishing setup" + auto-call `POST /onboard`. |
| `under_review`           | `route_product_status` ∈ {requested, under_review, created}       | "Razorpay is reviewing your settlement details" + poll.    |
| `needs_clarification`    | `route_product_status == needs_clarification`                     | Banner with Razorpay's `requirements[]` + dashboard CTA.    |
| `activated`              | `route_product_status == activated`                               | ✅ Green success screen. Settlements are live.              |
| `rejected`               | `route_product_status == rejected`                                | Failure screen + support link.                              |
| `suspended`              | `status == suspended` (gateway suspended the account)             | Suspended banner + support link.                            |

Do **not** branch off `status` alone — it remains `created` for the entire happy-path lifetime of an account.

---

## 2a. `GET /linked-account/details` — full Razorpay payload

Mirrors Razorpay's `GET /v2/accounts/:account_id` end-to-end. Use this when you need the full account view (profile, all addresses, legal_info, contact_info, apps, brand) rather than the lightweight local row from `GET /linked-account`. Side-effect: re-syncs the local row, so the cheap endpoint stays consistent.

Response (200) — Razorpay payload **as-is** plus two convenience keys:

```jsonc
{
  // ── Razorpay /v2/accounts/:id passthrough ────────────────────────
  "id": "acc_GLGeLkU2JUeyDZ",
  "type": "route",
  "status": "created",                  // gateway enum: created | suspended
  "email": "owner@acme.com",
  "phone": "9876543210",
  "reference_id": "123123",
  "business_type": "partnership",
  "legal_business_name": "Acme Corp",
  "customer_facing_business_name": "Acme Diner",
  "contact_name": "Gaurav Kumar",
  "created_at": 1611136837,            // unix epoch seconds (gateway)
  "notes": {},
  "profile": {
    "category": "food",
    "subcategory": "restaurant",
    "business_model": null,
    "addresses": {
      "registered": { "street1": "...", "city": "Bengaluru", "state": "KARNATAKA", "postal_code": 560034, "country": "IN" },
      "operation":  { "street1": "...", "city": "Bengaluru", "state": "KARNATAKA", "country": "IN" }
    }
  },
  "legal_info":   { "pan": "AAACL1234C", "gst": "18AABCU9603R1ZM" },
  "contact_info": {
    "chargeback": { "email": null, "phone": null, "policy_url": null },
    "refund":     { "email": null, "phone": null, "policy_url": null },
    "support":    { "email": null, "phone": null, "policy_url": null }
  },
  "apps":  { "websites": [], "android": [{ "url": null, "name": null }], "ios": [{ "url": null, "name": null }] },
  "brand": { "color": null },

  // ── Bittu convenience keys (NOT from Razorpay) ───────────────────
  "merchant_id":      "751c6d1d-…",
  "local_status":     "created",       // our enum: created | activated | suspended | rejected | deleted
  "route_product_status": "activated", // mirror of the local row's product status
  "effective_status": "activated"      // same derived value as GET /linked-account — see §2 table
}
```

Errors:
- **404** `{detail: "No linked account provisioned for this merchant"}` — call `/provision` first.
- **404** `{detail: "Linked account does not exist"}` — gateway returned BAD_REQUEST with `linked_account_id_does_not_exist` (deleted on dashboard / wrong env).
- **502 / 503** — upstream Razorpay 5xx after retries.

---

## 2b. `PATCH /linked-account` — update existing account

Mirrors Razorpay's `PATCH /v2/accounts/:account_id`. Every field is **optional**; send only what changes. `business_type` and `email` are immutable on Razorpay and are deliberately not accepted by this endpoint.

```jsonc
{
  "phone": "9876543210",                     // 8..15 digits after normalisation; leading 91 stripped
  "legal_business_name": "Acme Corp V2",     // 4..200
  "customer_facing_business_name": "Acme Diner",  // 1..255
  "reference_id": "partner_ref_123",         // 1..512 chars of [A-Za-z0-9_-]; auto-dropped if platform lacks route_code_support
  "contact_name": "Gaurav Kumar",            // 4..255 after sanitisation
  "notes": { "updated_by": "owner" },

  "category": "food",                         // profile.category
  "subcategory": "restaurant",                // profile.subcategory
  "business_model": "Restaurant chain",       // 1..255
  "addresses": {                              // any subset of slots
    "registered": { "street1": "...", "city": "Bengaluru", "state": "Karnataka", "postal_code": "560038", "country": "IN" },
    "operation":  { "street1": "...", "city": "Bengaluru", "state": "KA", "postal_code": "560047", "country": "IN" }
  },

  "pan": "BAACL1234C",                        // silently dropped if regex fails
  "gst": "10AABCU9603R1ZM",                   // silently dropped if regex fails

  "contact_info": {
    "chargeback": { "email": "ops@acme.com",     "phone": "9876543210" },
    "refund":     { "email": "refunds@acme.com", "phone": "9876543210" },
    "support":    { "email": "help@acme.com",    "phone": "9876543210", "policy_url": "https://acme.com/policy" }
  },
  "apps": { "websites": ["https://acme.com"] }
}
```

### Behaviour

- Empty body → 200 with the unchanged local row (no gateway call).
- All normalisations from `/provision` apply (state codes, country, phone, contact_name sanitisation, PAN/GST silent drop).
- `reference_id` retry-without-it path is the same as create.
- Success: 200 with the local `rzp_route_accounts` row reflecting the gateway PATCH response (the backend resyncs immediately).
- 404 if no linked account exists yet for this merchant — call `/provision` first.
- 400 for business validation (e.g. `phone` < 8 digits after normalisation, `contact_name` < 4 chars after sanitisation).
- 422 for Pydantic regex/length failures (see field constraints above).
- 502/503 if Razorpay returns 5xx after retries.

---

## 3. `POST /linked-account/onboard` — one-shot

Same body as `/provision` **plus** these KYC seed fields (all optional — they upsert into `merchant_kyc_*` before provisioning, so the FE can onboard a brand-new merchant in a single round-trip):

```jsonc
{
  "bank_account_number": "0123456789",   // REQUIRED for onboard (goes to /products PATCH, not to /accounts)
  "ifsc": "HDFC0001234",
  "beneficiary_name": "Acme Foods Pvt Ltd",
  "reference_id": "m_ab12cd34ef",
  "tnc_accepted": true,
  "notes": { },

  // KYC seed
  "legal_name": "Acme Foods Pvt Ltd",                              // 4..200
  "business_type": "private_limited",                              // proprietorship|partnership|llp|private_limited|public_limited|huf|trust|society|individual|other
  "pan": "ABCDE1234F",                                             // regex ^[A-Za-z]{5}\d{4}[A-Za-z]$
  "gstin": "29ABCDE1234F1Z5",                                      // regex ^[0-3][0-9][A-Za-z]{5}[0-9]{4}[A-Za-z][0-9][A-Za-z0-9]{2}$
  "contact_email": "ops@acme.com",                                 // 3..254
  "contact_phone": "9876543210",                                   // 8..15
  "registered_address": {
    "street1": "12 MG Road", "city": "Bengaluru",
    "state": "Karnataka", "postal_code": "560038", "country": "India"
  },

  "owner_name": "Ravi Kumar",
  "owner_role": "director",                                        // director|partner|proprietor|ubo|authorized_signatory
  "owner_email": "ravi@acme.com",
  "owner_phone": "9876543210",
  "owner_pan": "ABCDE1234F",
  "owner_dob": "1985-03-12",
  "owner_ownership_pct": 60.0,

  "bank_name": "HDFC Bank",
  "account_type": "current",                                        // savings|current|nro|nre

  // Profile + spec pass-throughs (same as /provision)
  "category": "food",
  "subcategory": "restaurant",
  "addresses": { "registered": { /* same as above */ } },
  "customer_facing_business_name": "Acme Diner",
  "contact_info": { /* same as above */ },
  "apps": { "websites": ["https://acme.com"] }
}
```

### Pydantic-enforced rejections (HTTP 422 if violated)

- `pan`, `owner_pan`: must match PAN regex.
- `gstin`: must match GSTIN regex.
- `legal_name`: 4..200 chars.
- `contact_email`: 3..254 chars.
- `contact_phone`: 8..15 chars.
- `reference_id`: 3..20 chars of `[A-Za-z0-9_-]`.
- `customer_facing_business_name`: 1..255.

Hand back **422 details** verbatim — they already include the exact field path.

---

## 4. Error surface the FE must handle

| HTTP | Body shape | When | UI suggestion |
|---|---|---|---|
| 200 | account row | success (incl. silent retry without `reference_id`) | proceed to stakeholder / product step |
| 400 | `{detail: "<message>"}` | input failed business validation (e.g. registered address empty, contact_name <4 chars after sanitisation, phone wrong length) | inline field error |
| 403 | `{detail: ...}` | missing `razorpay.route.write` permission | "contact owner" toast |
| 404 | `{detail: ...}` | sync called before provision | call `/provision` first |
| 409 | `{detail: ...}` | conflicting KYC state | usually means another tab/device already submitted KYC |
| 422 | `{detail: [{loc, msg, type}, …]}` | Pydantic — bad regex/length | map `loc` back to form field |
| 502 / 503 | `{detail: ...}` | upstream Razorpay 5xx after retries | "try again in a minute" |

### Specifically:

- **Duplicate-email adoption**: if Razorpay says "email already exists for account `<id>`", the backend silently adopts that account and returns 200. No FE handling needed.
- **`reference_id` not allowed**: if the platform doesn't have the `route_code_support` feature flag, backend retries without `reference_id` and returns 200. No FE handling needed. The `reference_id` field on the response may therefore differ from what you sent (or be null).
- **State / country casing**: never warn the user. Anything human (`"Karnataka"`, `"karnataka"`, `"KA"`, `"India"`, `"in"`) is accepted.

---

## 5. Minimal happy-path flow

```text
1. Owner fills: legal_name, business_type, pan, gstin, address, owner_*, bank_*
2. FE POST /linked-account/onboard with the full body
3. Poll GET /linked-account every ~15s until effective_status == "activated"
   (or subscribe to the linked_account WS push if your client wires that channel)
4. Once activated, transfers can be created via POST /transfers (separate spec)
```

For brownfield merchants whose KYC is already complete, send just:
```json
{ "bank_account_number": "...", "ifsc": "...", "beneficiary_name": "...", "tnc_accepted": true }
```

---

## 6. What did NOT change (so the FE doesn't accidentally re-do work)

- Endpoint paths and HTTP methods.
- Permission keys.
- Response shape of `GET /linked-account` and `POST /linked-account/sync`.
- Idempotency: every endpoint is safe to retry.
- The bank account number is still **in-memory only** — backend stores `last4 + sha256(account_number) + ifsc`.

---

## 7. Quick smoke test (curl)

```bash
TOKEN="<jwt>"
HOST="https://<your-host>"

curl -sS -X POST "$HOST/api/v1/razorpay-route/linked-account/provision" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_facing_business_name": "Acme Diner",
    "contact_info": { "support": { "email": "help@acme.com", "phone": "9876543210" } },
    "apps":         { "websites": ["https://acme.com"] }
  }' | jq
```

Expected: 200 with the account row (or 400 with a clear `detail` if KYC is incomplete).
