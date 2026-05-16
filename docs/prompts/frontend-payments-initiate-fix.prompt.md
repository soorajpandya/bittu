# Frontend instructions — `/payments/initiate` is fixed (backend commit `ee1eb43`, deployed)

The backend `POST /api/v1/payments/initiate` no longer 500s. It now does exactly what your Phase 1–8 flow expects: returns the Razorpay intent + QR fields for online orders, idempotently. **No frontend rewrite required to unblock — but read §3 to clean up the integration.**

---

## 1. What changed on the backend

- Old behaviour: `POST /payments/initiate` raised `TypeError` (wrong kwargs passed to the service) → HTTP 500 in ~1.3s. Even if that were fixed, the legacy service would 400 because `/orders/checkout` had already created a `pending` payment.
- New behaviour:
  - Accepts your exact payload as-is (`order_id`, `payment_mode`, `amount`, `currency`, `customer_phone`, `customer_name`, `tip`, plus `Idempotency-Key` header). Extra fields ignored.
  - `payment_mode` aliases `online | razorpay | gateway | netbanking` all normalise to `online`. `cash | upi | card | wallet` go through the legacy completion path.
  - `amount` auto-detects paise vs rupees against `orders.total_amount`. You can keep sending paise — no change needed.
  - For **online**, the route looks up the payment + Razorpay intent that `/orders/checkout` already created and **returns the same QR**. Idempotent: same `Idempotency-Key` (and same order) ⇒ same response.
  - If checkout's gateway call had dropped (`razorpay.error` in checkout response), `/payments/initiate` will create the intent on demand. So a single retry of `/payments/initiate` heals a flaky checkout.
  - Terminal payment states (`completed | failed | refunded`) short-circuit — stop polling immediately.
  - Razorpay outage now returns **502**, not 500.

---

## 2. Response shape (what you actually receive now)

`POST /api/v1/payments/initiate` → `200 OK`:

```jsonc
{
  "payment_id":         "9c8a…",         // UUID
  "order_id":           "1a2b…",         // echo
  "method":             "online",        // normalised
  "status":             "pending",       // payments.status: pending|initiated|completed|failed|refunded
  "amount":             250.00,          // rupees (float)
  "amount_paise":       25000,           // int
  "currency":           "INR",
  "razorpay_order_id":  "order_OxYz…",
  "qr_id":              "qr_OxYz…",
  "qr_image_url":       "https://rzp.io/.../qr.png",   // preferred for render
  "qr_image_content":   "upi://pay?pa=…&am=250&cu=INR&tn=…",  // UPI deep link fallback
  "qr_close_by":        "2026-05-16T11:21:27+00:00",   // ISO 8601 UTC
  "qr_status":          "active",        // active | closed | expired
  "idempotent":         true             // true on replay
}
```

For `cash | upi | card | wallet` the response is the legacy shape — no `qr_*` fields, `status` will already be `completed`:

```jsonc
{
  "payment_id": "…", "status": "completed", "method": "cash",
  "amount": 250.0, "razorpay_order_id": null
}
```

Error codes you may see now (none are 500 from this route any more):
- `400` — bad `payment_mode`, or `amount` doesn't fit the order total.
- `404` — order not found / not in your tenant.
- `502` — Razorpay rejected or timed out.

---

## 3. What the frontend should change

### 3.1 Stop treating `/payments/initiate` as a separate step (recommended)

Your current flow is `POST /orders/checkout` → `POST /payments/initiate` → poll. That extra `/payments/initiate` hop is no longer required, because checkout already returns the intent inline:

```jsonc
// POST /api/v1/orders/checkout response when payment_method == "online"
{
  "id": "…", "status": "Pending", "payment_id": "…", "payment_status": "pending",
  "total_amount": 250.0, "items": [...],
  "razorpay": {                              // ← consume this directly
    "razorpay_order_id": "order_…",
    "amount":            25000,              // paise
    "currency":          "INR",
    "qr_id":             "qr_…",
    "qr_image_url":      "https://…png",
    "qr_image_content":  "upi://pay?…",
    "qr_close_by":       "2026-…+00:00"
  }
}
```

**New canonical flow:**
1. `POST /orders/checkout` with header `X-Idempotency-Key: <uuid v4>`. Read `response.razorpay`.
2. If `response.razorpay` is present and has `qr_image_url` → render the QR immediately.
3. If `response.razorpay == {"error": "intent_creation_failed"}` (gateway dropped) → fall back to `POST /api/v1/payment-intents/{order_id}/refresh` (no body). Returns the same `IntentOut`.
4. Poll `GET /api/v1/payment-intents/{order_id}` every 3s until `payment_status ∈ {completed, failed, refunded}` or `qr_close_by` elapses (10-min hard cap).
5. On `completed` → `GET /api/v1/orders/{order_id}` → render receipt.

This drops `/payments/initiate` from the happy path entirely and removes one round-trip.

### 3.2 If you keep `/payments/initiate` (zero-churn option)

It now works. Keep your existing code — just be aware:
- Send the **same** `Idempotency-Key` on retries. Each unique key, however, will return the **same intent** because idempotency on the Razorpay side is keyed on `(merchant_id, internal_order_id)`, not the header. So even a fresh key won't create a duplicate Razorpay order.
- After the first 200, your poller should use `GET /api/v1/payment-intents/{order_id}` — don't keep re-POSTing `/initiate`.
- `qr_image_url` may be `null` only if Razorpay's QR creation failed (rare); fall back to rendering `qr_image_content` (UPI deep link) yourself with any client-side QR-code library.

### 3.3 Add response-body logging
You mentioned wanting to surface the actual server error in the Flutter console. Worth keeping for the next regression. Suggested:

```dart
// in your dio/http interceptor
onError: (e, handler) {
  debugPrint(
    '[HTTP-ERR] ${e.requestOptions.method} ${e.requestOptions.path} '
    '${e.response?.statusCode} body=${e.response?.data}',
  );
  handler.next(e);
}
```

This would have shown the `TypeError` immediately and saved both of us an hour.

### 3.4 Money & precision
- Paise everywhere arithmetic happens. Format only at the render boundary with `NumberFormat.simpleCurrency(locale: 'en_IN', name: 'INR')`.
- Always render the server's `amount` / `total_amount` on receipts. Never the client estimate.

### 3.5 Idempotency-Key lifecycle (unchanged, but worth re-checking)
- Generate `Idempotency-Key` **once** when the user taps Pay.
- Reuse the same key on every retry of `/orders/checkout` (and `/payments/initiate` if you keep it) until you get a 2xx.
- Generate a **new** key only when the user taps Pay again after a hard failure.

---

## 4. Test matrix to run on the new build

| Scenario                                                       | Expected                                                                 |
|---                                                             |---                                                                       |
| Online order, checkout returns QR inline                       | Render `response.razorpay.qr_image_url`, poll, payment completes         |
| Online order, checkout returns `razorpay.error`                | `/payment-intents/{id}/refresh` succeeds, QR appears                     |
| Retry `/orders/checkout` after timeout with **same** idem key  | 200 with `idempotent: true`, **no duplicate order**                      |
| Retry `/orders/checkout` after timeout with **different** key  | New order — bug in your retry logic, prove it doesn't happen             |
| Call `/payments/initiate` twice for same order                 | Both 200, identical `razorpay_order_id`, `idempotent: true` on the 2nd   |
| QR expires (`qr_close_by` passed)                              | Stop polling, offer "Generate new QR" → `/payment-intents/{id}/refresh`  |
| Cash payment via `/orders/checkout`                            | Receipt rendered immediately, no QR                                      |
| Payment fails on Razorpay side                                 | Poll sees `payment_status: failed`, stops, shows failure UI              |
| Backend Razorpay outage                                        | Checkout returns `razorpay.error`; refresh returns 502; degrade UI       |

---

## 5. Hard constraints (still)

- Never call Razorpay APIs directly from the client.
- Never call `/api/v1/webhooks/*` from the client.
- Never roll a new `Idempotency-Key` mid-retry.
- Never edit/delete an order once `payment_status == completed`.

---

## 6. Deliverables for this round

1. **No-op option:** keep current code; verify the 500 is gone and the QR renders end-to-end against `https://api.<your-domain>/api/v1/payments/initiate`.
2. **Cleanup option (preferred):** delete the `/payments/initiate` POST from the online flow; consume `response.razorpay` from `/orders/checkout` directly; keep `/payment-intents/{id}/refresh` as the only fallback. Net result: one fewer round-trip per checkout, and the intent never gets recreated on a different idem-key boundary.
3. Add the response-body logger from §3.3 — non-negotiable for future debugging.
4. Confirm all rows in the §4 matrix pass.

Backend commit: `ee1eb43` on `main`, deployed to EC2 `bittu.service` at 10:51 UTC. Service is `active (running)`.
