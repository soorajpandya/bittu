# Frontend Implementation Prompt — Geo-fence, Auto Route Split & Per-Merchant Reports

> **Context:** Backend release `85cec6f` shipped three things the frontend now needs to surface. This prompt is the contract — endpoints, request bodies, response shapes, error codes, and UX rules. Implement against `https://www.bittupos.com` (or the staging equivalent). All routes require the standard Bearer JWT (Supabase access token) in `Authorization: Bearer <token>`.

---

## 0. Global rules

- **Money:** All amounts in responses are **rupees** as JSON numbers (e.g. `123.45`), except Razorpay-native fields suffixed `_paise` which are integers (e.g. `12345`). Format `_paise` for display by dividing by 100.
- **Dates:** ISO `YYYY-MM-DD` for date params, ISO 8601 (`Z`) for timestamps in responses.
- **Permissions:** Backend enforces `reports.read` / `reports.export` / `order.create` / `payment.create`. Hide menu items if `me.permissions` does not contain the required key (do not rely on the call failing).
- **Tenant scoping:** Never send `merchant_id` / `restaurant_id` in any request. The backend derives it from the JWT. Sending it is silently ignored, but it must never appear in our codebase.
- **Error envelope (FastAPI default):** `{ "detail": "<human-readable string>" }` with HTTP 4xx/5xx. Toast `detail` verbatim unless mapped below.

---

## 1. Geo-fence on checkout / payment initiate

### Why
Each branch can set a GPS centre + radius (default 100 m, opt-in via `geofence_enabled`). When enabled, customers paying on their own device must be physically inside the radius. When the merchant has **not** configured GPS or has not flipped the toggle, the check is silently skipped — so this is fully backwards compatible.

### What changed
Two existing endpoints accept **two new optional body fields**:

| Field | Type | Notes |
|---|---|---|
| `customer_lat` | `number` (decimal degrees, WGS84) | Optional. Omit if the user denied geolocation. |
| `customer_lng` | `number` (decimal degrees, WGS84) | Optional. Send both or neither. |

### Affected endpoints

#### 1a. `POST /api/v1/orders/checkout`
Existing fields unchanged. Add the two new fields when available.

```http
POST /api/v1/orders/checkout
Authorization: Bearer <jwt>
Content-Type: application/json
X-Idempotency-Key: <uuid-v4>      // strongly recommended

{
  "items": [{ "item_id": 42, "quantity": 2, "unit_price": 199.00 }],
  "total_amount": 470.00,
  "subtotal": 398.00,
  "tax_amount": 72.00,
  "payment_method": "online",
  "order_type": "dine_in",
  "source": "customer_app",
  "branch_id": "<uuid>",
  "table_id": "<uuid-or-null>",
  "customer_name": "Anita",
  "customer_phone": "+91…",

  // NEW — geo-fence (optional pair)
  "customer_lat": 12.97162345,
  "customer_lng": 77.59456712
}
```

#### 1b. `POST /api/v1/payments/initiate`
```http
POST /api/v1/payments/initiate
Authorization: Bearer <jwt>
Content-Type: application/json

{
  "order_id": "<uuid>",
  "payment_mode": "online",      // cash | upi | card | wallet | online
  "amount": 47000,               // paise OR rupees — backend auto-detects
  "currency": "INR",
  "customer_name": "Anita",
  "customer_phone": "+91…",

  // NEW — geo-fence (optional pair)
  "customer_lat": 12.97162345,
  "customer_lng": 77.59456712
}
```

### Error to handle
HTTP **422** with `detail` matching the regex `Outside branch geofence:`. UX:
- Block the checkout/pay button (do **not** retry automatically).
- Toast `detail` verbatim — the backend computes both distance and radius into the message, e.g.  
  *"Outside branch geofence: you are 312 m away (allowed 100 m). Please pay at the counter."*
- Offer a **"Pay at counter"** secondary CTA that switches `payment_mode` to `cash` and retries (cash is exempt from geofence by design at the merchant's discretion — backend still enforces if they want; treat the second 422 the same way).

### Geolocation UX rules
1. Request `navigator.geolocation.getCurrentPosition` with `{ enableHighAccuracy: true, timeout: 6000, maximumAge: 30000 }` **only on the customer-facing flows** (QR scan ordering, dine-in self-pay). POS/staff flows must **not** send coordinates.
2. If the user denies permission, **omit both fields** (do not send `null`, do not send `0`) — the backend will skip the check.
3. Cache the last fix for 30 s to avoid blocking UI on repeated taps.
4. Never store the coordinates in localStorage / analytics — they are PII.

---

## 2. Auto Route split (no frontend work, just awareness)

When a Razorpay payment is captured, the backend now **automatically** splits `(gross − Bittu fee − GST)` to the merchant's linked account via Razorpay Route. **Nothing changes in the payment flow from the frontend's perspective.** Two facts to communicate to merchants in the dashboard:

- If the merchant has finished KYC and their linked account is `activated`, money lands in their bank account on the standard Razorpay settlement cycle (T+2/T+3).
- If KYC is incomplete, funds remain on the Bittu master account and a manual payout is required. Show a yellow banner *"Complete KYC to enable automatic settlements"* when the merchant profile's `linked_account.status !== 'activated'` (reuse the existing `/api/v1/razorpay-route/account` endpoint).

---

## 3. Per-Merchant Reports & Invoices

New router: prefix **`/api/v1/merchant-reports`**. All routes require `reports.read` (or `reports.export` for CSV). All are scoped to the caller's restaurant — there is no merchant selector.

### 3.1 `GET /summary` — Dashboard KPIs

```
GET /api/v1/merchant-reports/summary
    ?window=today|week|month|lifetime|custom
    &from_date=2026-05-01           // required when window=custom
    &to_date=2026-05-15             // required when window=custom
    &currency=INR
```

Response:
```jsonc
{
  "window": "month",
  "from_date": "2026-05-01",
  "to_date":   "2026-05-15",
  "pnl": {
    "orders_count": 142,
    "gross_sales": 84210.00,
    "discounts": 1200.00,
    "tax": 12950.00,
    "cogs": 31200.00,
    "refunds_count": 3,
    "refunds_amount": 540.00,
    "disputes_count": 0,
    "chargebacks_amount": 0,
    "fees": 1264.00,        // Bittu platform fee for the window
    "gst":  227.52,         // GST on platform fee
    "payments_in": 84210.00,
    "settlements_out": 79980.00,
    "net": 81254.48
  },
  "wallet": {
    "cash_collected": 23400.00,
    "cash_refunded":  0.00,
    "online_captured": 60810.00,
    "settle_pending_net": 15200.00,
    "settle_lifetime_net": 64780.00,
    "fees_lifetime": 1264.00,
    "tx_count": 142
    // …additional fields from MerchantWalletService
  }
}
```

**UX:** Build a four-tile header (*Today / This Week / This Month / Lifetime*). On click, call this endpoint with the matching `window`. Cache for 30 s per window.

### 3.2 `GET /wallet` — Wallet snapshot only
```
GET /api/v1/merchant-reports/wallet?as_of_date=2026-05-15
```
Returns just the `wallet` block above. Use this for a small "Wallet" widget that auto-refreshes every 60 s.

### 3.3 `GET /transactions` — Per-payment ledger (paginated)
```
GET /api/v1/merchant-reports/transactions
    ?from_date=2026-05-01
    &to_date=2026-05-15
    &method=online           // optional: cash|upi|card|wallet|online
    &status=completed        // optional: completed|pending|initiated|failed|refunded
    &limit=50                // 1..500
    &offset=0
```

Response:
```jsonc
{
  "from_date": "2026-05-01",
  "to_date":   "2026-05-15",
  "limit": 50, "offset": 0, "count": 23,
  "items": [
    {
      "payment_id": "f1a2…",
      "order_id":   "9c0b…",
      "created_at": "2026-05-15T10:21:44.512Z",
      "method":     "online",
      "status":     "completed",
      "amount":     470.00,
      "currency":   "INR",
      "razorpay_payment_id": "pay_OabC…",
      "razorpay_order_id":   "order_OabC…",
      "customer_name":  "Anita",
      "customer_phone": "+91…"
    }
  ]
}
```

**UX:** Standard table with sticky header. Each row links to `/orders/:order_id` (existing order detail page) and exposes a "Download invoice" button → §3.6. Default window = last 30 days. Use infinite scroll: when 80 % scrolled, request next page using `offset += limit`.

### 3.4 `GET /transactions.csv` — CSV export
```
GET /api/v1/merchant-reports/transactions.csv
    ?from_date=2026-05-01
    &to_date=2026-05-15
    &method=…&status=…
```
Returns `text/csv` with `Content-Disposition: attachment; filename="transactions_<from>_<to>.csv"`. Capped at 50 000 rows server-side.

**UX:** "Export CSV" button. Hit the endpoint and let the browser handle the download — do **not** parse the body in JS. Disable the button if the selected window would exceed 90 days (advise narrowing first).

### 3.5 `GET /settlements` — Razorpay settlements
```
GET /api/v1/merchant-reports/settlements
    ?from_date=2026-05-01
    &to_date=2026-05-15
    &limit=50&offset=0
```

Response:
```jsonc
{
  "from_date": "2026-05-01",
  "to_date":   "2026-05-15",
  "count": 8,
  "items": [
    {
      "settlement_id": "setl_OabC…",
      "amount_paise":  6478000,        // ← paise; divide by 100 for display
      "fees_paise":    12950,
      "tax_paise":     2331,
      "utr":           "HDFCN52026051512345",
      "status":        "processed",    // pending | processed | failed | reversed
      "created_at":    "2026-05-15T03:30:00Z",
      "settled_at":    "2026-05-15T11:45:12Z"
    }
  ]
}
```

**UX:** Table with columns: Date · UTR · Net Amount (₹) · Fees (₹) · Tax (₹) · Status (badge). Status colours: `processed` = green, `pending` = amber, `failed`/`reversed` = red.

### 3.6 `GET /invoice/{order_id}.pdf` — Customer tax invoice
```
GET /api/v1/merchant-reports/invoice/9c0b1a2e-….pdf
```
Returns `application/pdf` (~30 KB, inline). Use it for:
- "Download invoice" button in the transactions table.
- "Share via WhatsApp" — fetch the PDF as a Blob, then `URL.createObjectURL` + share intent.

Errors:
- **404** `{ "detail": "order not found for this merchant" }` — order belongs to a different tenant or does not exist. Toast and keep the UI in place.

### 3.7 `GET /saas-invoice/{year}/{month}.pdf` — Bittu's monthly SaaS bill
```
GET /api/v1/merchant-reports/saas-invoice/2026/5.pdf?currency=INR
```
Idempotent: the same call always returns the same invoice number for that `(year, month)` pair, persisting a row in `bittu_saas_invoices` on the first call. Use it for the **Billing → Invoices** section of the merchant dashboard.

**UX:** Render a 12-month grid for the current year + previous year. Each cell is a button → opens the PDF in a new tab. Future months in the current year are disabled (no data yet).

Validation errors to map:
- **422** `{ "detail": "month must be 1..12" }` — should never happen if the grid is gated; log to Sentry.
- **422** `{ "detail": "year out of range" }` — same.

---

## 4. Suggested information architecture

Add to the merchant dashboard sidebar under a new group **Reports**:

```
Reports
├── Overview            → calls /summary?window=month, with window switcher
├── Wallet              → /wallet, auto-refresh 60 s
├── Transactions        → /transactions + CSV export button
├── Settlements         → /settlements
└── Invoices
     ├── Customer invoices → search bar by order_id, calls /invoice/{id}.pdf
     └── Bittu SaaS bills  → /saas-invoice grid
```

Permission gates:
- Hide the whole **Reports** group when the user lacks `reports.read`.
- Hide the "Export CSV" buttons when the user lacks `reports.export`.

---

## 5. Acceptance criteria (tick all before shipping)

- [ ] Customer-app QR-order flow sends `customer_lat`/`customer_lng` when permission is granted and omits both when denied.
- [ ] A 422 `Outside branch geofence:` response is shown verbatim and does **not** trigger any retry.
- [ ] POS / staff flows never send geo coordinates.
- [ ] Dashboard `Overview` tab renders the four window tiles and switches `window` correctly without page reload.
- [ ] Transactions page paginates via `offset` and shows a "loading more…" indicator at the bottom.
- [ ] CSV button hits `/transactions.csv` and triggers a browser download (no in-app rendering).
- [ ] Settlements page formats `*_paise` fields correctly as rupees.
- [ ] Customer invoice PDF opens inline in a new tab; 404 is toasted, not crashed.
- [ ] SaaS invoice grid disables future months; opens the PDF for past months.
- [ ] No request anywhere sends `merchant_id` / `restaurant_id` as a query param or body field.
- [ ] All new endpoints route through the existing auth interceptor (Bearer token attached, 401 → re-login).

---

## 6. Reference: cURL smoke tests

```bash
# Summary (this month)
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://www.bittupos.com/api/v1/merchant-reports/summary?window=month"

# Last 7 days of transactions
curl -sS -H "Authorization: Bearer $TOKEN" \
  "https://www.bittupos.com/api/v1/merchant-reports/transactions?from_date=2026-05-08&to_date=2026-05-15&limit=50"

# CSV export
curl -sS -H "Authorization: Bearer $TOKEN" -o txns.csv \
  "https://www.bittupos.com/api/v1/merchant-reports/transactions.csv?from_date=2026-05-01&to_date=2026-05-15"

# Customer invoice PDF
curl -sS -H "Authorization: Bearer $TOKEN" -o invoice.pdf \
  "https://www.bittupos.com/api/v1/merchant-reports/invoice/<order_uuid>.pdf"

# Bittu SaaS invoice (May 2026)
curl -sS -H "Authorization: Bearer $TOKEN" -o saas_may.pdf \
  "https://www.bittupos.com/api/v1/merchant-reports/saas-invoice/2026/5.pdf"

# Checkout with geo
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{ "items":[…], "total_amount":470, "payment_method":"online",
        "order_type":"dine_in", "source":"customer_app",
        "branch_id":"<uuid>",
        "customer_lat":12.97162345, "customer_lng":77.59456712 }' \
  "https://www.bittupos.com/api/v1/orders/checkout"
```

---

**Deliver:** PR against the frontend repo implementing the routes, components, permission gates, geolocation hook, and acceptance-criteria E2E specs. Tag @backend on the PR for contract review before merge.
