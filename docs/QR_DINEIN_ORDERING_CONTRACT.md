# QR Table Ordering — Frontend API Contract

End-to-end contract for the customer-facing QR dine-in flow: **scan → menu →
cart → order → pay (post-paid) → settle**. All `/dinein/qr/*` endpoints are
**public** (no JWT); they authenticate with the `session_token` returned by
`scan`. Base path: `/api/v1`.

> **Standardize on `/dinein/*`.** Do **not** mix the legacy `/tables/qr/*`
> endpoints or the admin `/tables/*` (JWT) endpoints into the customer flow.
> Use `/dinein/qr/*` for everything below.

---

## 0. The QR payload

The table QR encodes `restaurant_id` and `table_id`. The client generates a
stable `device_id` (persist it locally so refreshes reuse the session).

---

## 1. Scan — `POST /api/v1/dinein/qr/scan`

```json
{ "restaurant_id": "<uuid>", "table_id": "<uuid>", "device_id": "<string>", "session_token": "<optional, for restore>" }
```

Response (relevant fields):

```json
{ "session_id": "<uuid>", "session_token": "<string>", "table": {...}, "restaurant": {...}, "is_new": true, "active_order_id": null }
```

**Persist `session_token`** — it authenticates every later call. On reconnect,
send it back as `session_token` to restore the same session.

---

## 2. Menu — `GET /api/v1/dinein/qr/menu?restaurant_id=<uuid>`

Returns `categories`, `items`, `variants`, `addons`, `extras`,
`modifierGroups`, `combos`, `comboItems`.

> **CRITICAL — item_id source (this is the #1 cause of "can't add items").**
> The `cart/add` endpoint requires `item_id` = the **integer `Item_ID`** from a
> menu **item**. Do **not** send a category id, a variant UUID, or a **combo**
> UUID as `item_id`. Combos are **not** addable via `cart/add` today (they carry
> UUID ids) — add their component items instead, or treat combos as unsupported
> in the cart until combo support ships.

---

## 3. Cart

Add — `POST /api/v1/dinein/qr/cart/add`

```json
{
  "session_token": "<string>",
  "item_id": 1234,                 // integer Item_ID from the menu — REQUIRED
  "quantity": 1,
  "variant_id": "<optional uuid>",
  "addons": [{ "id": "...", "name": "...", "price": 10 }],
  "extras": [{ "id": "...", "name": "...", "price": 5 }],
  "notes": "<optional>",
  "device_id": "<optional>",
  "request_id": "<optional idempotency key>"
}
```

`422` means the payload failed validation (usually `item_id` was not an
integer). `Item not available` means the resolved `Item_ID` has
`Available_Status = false` or doesn't exist.

Other cart ops: `POST /qr/cart/update`, `POST /qr/cart/remove`,
`POST /qr/cart/clear?session_token=...`, `GET /qr/cart?session_token=...`.

---

## 4. Place order — `POST /api/v1/dinein/qr/place-order`

```json
{ "session_token": "<string>", "notes": "<optional>", "customer_name": "<optional>", "customer_phone": "<optional>", "request_id": "<optional>" }
```

Sends the cart to the kitchen immediately (post-paid). Multiple place-order
calls **append** to the same active order for the session. Track status via
`GET /qr/order-status?session_token=...`.

---

## 5. Bill — `GET /api/v1/dinein/qr/bill?session_token=<string>`

```json
{ "session_id": "...", "status": "active", "orders": [...], "grouped_items": [...],
  "grand_total": 540.0, "paid_total": 0.0, "remaining_amount": 540.0, "payments": [] }
```

Poll this to render the running bill. Poll every ~5s while the table is open.

---

## 6. Pay (post-paid) — `POST /api/v1/dinein/qr/pay`

Call this when the customer chooses to pay. Charges the **remaining** bill.
Routing is **plan-driven** (chosen by the merchant at onboarding) and returned
as `mode` — the FE must branch on it.

Request:

```json
{ "session_token": "<string>", "customer_name": "<optional>", "customer_phone": "<optional>" }
```

### 6a. `mode: "integrated"` (growth / enterprise plans — Razorpay Route)

```json
{
  "mode": "integrated",
  "session_id": "...",
  "order_id": "...",
  "payment_id": "...",
  "amount": 540.0,
  "amount_paise": 54000,
  "currency": "INR",
  "razorpay_order_id": "order_XXXX",
  "qr_id": "qr_XXXX",
  "qr_image_url": "https://...png",
  "qr_image_content": "upi://pay?pa=...&am=540.00&cu=INR",
  "qr_close_by": "2026-07-03T12:34:56+00:00",
  "settlement": "automatic",
  "poll_status_after_ms": 3000,
  "message": "Scan the UPI QR or open your UPI app to pay. Payment confirms automatically."
}
```

FE action: render `qr_image_url` (or deep-link `qr_image_content` to open the
UPI app), then **poll `GET /qr/payment-status`**. Settlement is confirmed
**automatically** by the `payment.captured` webhook — no manual step.

### 6b. `mode: "own_upi"` (starter / business plans — merchant's own UPI)

```json
{
  "mode": "own_upi",
  "session_id": "...",
  "order_id": "...",
  "amount": 540.0,
  "amount_paise": 54000,
  "currency": "INR",
  "upi_intent": "upi://pay?pa=<merchant-vpa>&am=540.00&cu=INR&pn=...",
  "qr_image": "data:image/png;base64,...",
  "settlement": "manual",
  "message": "Pay to the restaurant's UPI, then ask staff to confirm your payment."
}
```

FE action: render `qr_image` / deep-link `upi_intent`. Money goes **directly to
the restaurant's UPI** — Bittu gets **no webhook**. Settlement is **manual**:
staff confirm the payment from the POS. The customer app should show a "waiting
for staff to confirm" state and keep polling `payment-status`.

Errors: `This session has nothing left to pay` (already settled),
`Place an order before paying` (empty session), `Online payments are not
enabled for this restaurant yet` (integrated merchant not Route-activated),
`This restaurant has not set up its UPI payment QR yet` (own_upi merchant with
no static QR).

---

## 7. Payment status — `GET /api/v1/dinein/qr/payment-status?session_token=<string>`

```json
{ "session_id": "...", "status": "active", "grand_total": 540.0, "paid_amount": 540.0, "remaining_amount": 0.0, "is_paid": true, "payments": [...] }
```

Poll every ~3s after initiating payment. When `is_paid == true`, show the
success screen. For integrated mode this flips automatically once the webhook
lands; for own_upi it flips when staff confirm. When fully paid, the table is
auto-vacated server-side.

---

## Flow summary

```
scan → menu → cart/add (item_id = integer Item_ID) → place-order
      → (repeat add/place-order as needed) → pay → poll payment-status → paid
```

## Notes / current limitations

- **Post-paid only** — food is sent to the kitchen at place-order; payment is
  collected at the end.
- **Combos** are not addable via `cart/add` (UUID ids). Add component items.
- **Intent amount is captured at `pay` time.** If the customer adds more items
  after calling `/qr/pay` (integrated), regenerate by calling `/qr/pay` again
  only if `remaining_amount` changed and no capture happened yet.
- **own_upi settlement is manual** by design (merchant's own VPA, manual
  reconciliation plan).
