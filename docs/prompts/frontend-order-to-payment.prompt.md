# Prompt — Implement "Order Creation → Payment" flow on the frontend

You are implementing the end-to-end POS / customer flow:
**build cart → checkout → (if online) show Razorpay QR/intent → wait for payment confirmation → render receipt**.

The backend is the Bittu FastAPI server. Do NOT invent endpoints — only call the ones listed below. All requests are authenticated with a Bearer JWT issued by `/api/v1/auth/login`. All amounts are in INR; backend stores rupees as `numeric` and paise as `int` (Razorpay side only).

---

## 1. Required backend endpoints

Base URL: `/api/v1`

### 1.1 Create order (idempotent checkout)
- `POST /orders/checkout`
- Headers:
  - `Authorization: Bearer <jwt>`
  - `X-Idempotency-Key: <uuid v4>` ← **mandatory**. Generate once per checkout attempt, reuse on retry. Same key + same auth = same order, server returns `idempotent: true` on replay.
- Body (`CheckoutIn`):
  ```json
  {
    "items": [
      { "item_id": "<uuid|int>", "quantity": 2, "variant_id": null, "addons": [], "notes": null }
    ],
    "total_amount": 0,                 // client hint; server recalculates authoritatively
    "payment_method": "cash | upi | card | wallet | online",
    "order_type":     "pos | dine_in | takeaway | delivery",
    "source":         "pos",
    "subtotal": null, "discount_amount": null, "tax_amount": null, "service_charge": null,
    "customer_id": null, "customer_name": null, "customer_phone": null,
    "delivery_address": null, "notes": null,
    "branch_id": null, "table_id": null, "table_number": null,
    "coupon_id": null, "coupon_code": null
  }
  ```
- 200 response contains the committed order **plus**, when `payment_method` normalises to `online` (aliases: `online | razorpay | gateway | netbanking`), an extra `razorpay` object:
  ```json
  {
    "id": "<order_uuid>",
    "status": "Pending",
    "total_amount": 250.00,
    "items": [...],
    "payment": { "id": "<payment_uuid>", "status": "pending", "method": "online" },
    "razorpay": {
      "razorpay_order_id": "order_xxx",
      "amount": 25000,           // paise
      "currency": "INR",
      "qr_id": "qr_xxx",
      "qr_image_url": "https://...png",
      "qr_image_content": "upi://pay?...",
      "qr_close_by": "2026-05-16T12:34:56+00:00"
    }
  }
  ```
- If gateway call dropped, `razorpay` will be `{"error": "intent_creation_failed"}` — frontend MUST then call the refresh endpoint (1.3).
- `payment_method` values `upi | card | wallet` are POS-side cash-equivalents and DO NOT create a Razorpay intent. Only `online` does.

### 1.2 Read intent / poll for payment
- `GET /payment-intents/{order_id}` → returns `IntentOut`:
  ```ts
  {
    internal_order_id, razorpay_order_id,
    amount_paise, amount_paid_paise, amount_due_paise, currency,
    status,                       // rzp_order state: created|attempted|paid
    qr_id, qr_image_url, qr_image_content, qr_status, qr_close_by,
    payment_status,               // payments.status: pending|initiated|completed|failed|refunded
    razorpay_payment_id
  }
  ```
- `GET /payment-intents/{order_id}/qr` → `QrOut` (just the QR + received counters).
- Poll at most every 3s while `payment_status ∈ {pending, initiated}`. Stop on `completed | failed | refunded`, or when `qr_close_by` elapses, or after a 10-minute hard cap.

### 1.3 Refresh a missing/failed intent
- `POST /payment-intents/{order_id}/refresh`
- Call when checkout response had `razorpay.error` OR when `GET /payment-intents/{order_id}` returns 404 for an online order.
- Returns the same `IntentOut`. Idempotent — never creates a duplicate Razorpay order.
- 409 = payment already terminal; 400 = order is not `online`; 502 = gateway down (retry with backoff).

### 1.4 Get the final order (for receipt)
- `GET /orders/{order_id}` → full order with `items[]`. Use after payment is confirmed.

> Do NOT call `POST /payments/initiate`, `POST /payments/verify`, or any `/webhooks/*` route from the frontend. Webhooks are backend-only.

---

## 2. Frontend state machine

```
 IDLE
   └── user clicks "Pay"
         │
         ▼
 SUBMITTING_CHECKOUT  ──(error)──►  CHECKOUT_FAILED  ──(retry, SAME idem key)──► SUBMITTING_CHECKOUT
   │
   ├── method != online  ─►  ORDER_DONE (cash-equivalent: print receipt immediately)
   │
   └── method == online
         │
         ▼
 INTENT_READY  (render QR from response.razorpay.qr_image_url)
   │
   ├── response.razorpay.error  ─►  call POST /payment-intents/{id}/refresh
   │                                  └── success ─► INTENT_READY
   │                                  └── 502/timeout ─► retry w/ exp backoff (3 tries)
   │
   ▼
 WAITING_FOR_PAYMENT  (poll GET /payment-intents/{id} every 3s, or subscribe to WS — §3)
   │
   ├── payment_status == "completed"  ─► PAID  ─► GET /orders/{id} ─► RECEIPT
   ├── payment_status == "failed"     ─► PAYMENT_FAILED (offer retry → refresh)
   ├── qr_close_by elapsed             ─► EXPIRED (offer "regenerate QR" → refresh)
   └── 10-min cap                      ─► TIMEOUT (offer regenerate or cancel)
```

### Idempotency rules
- Generate the `X-Idempotency-Key` **once** when the user clicks Pay; persist it in component state.
- On any network error / 5xx / timeout during `POST /orders/checkout`, retry with the **same** key. Never roll a new key on retry — that's how duplicates happen.
- After a 2xx response, discard the key (or keep it so a stale retry from a flaky network still returns the same order).

### Money & precision
- Display amounts using `Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR' })`.
- Compute totals in **paise** (integer) on the frontend if you must do math, then format. Never use floats for arithmetic.
- The server is authoritative for totals — show the server's `total_amount` in the receipt, not the client's pre-checkout estimate.

---

## 3. Realtime (preferred over polling once you've shipped polling)

Backend has WebSocket fan-out from Postgres `supabase_realtime` publication. Tables already in the publication: `orders`, `payments`, `bittu_settlements`, etc. (REPLICA IDENTITY FULL).

- After checkout succeeds, subscribe to a per-order or per-merchant channel and react to `payments` row updates (status → completed/failed).
- Keep the 3s poll as a fallback. If WS disconnects, fall back to polling until reconnect.

(Confirm channel naming with backend before wiring — see `app/realtime/__init__.py`.)

---

## 4. Error handling matrix

| Endpoint                              | Status | Frontend action                                                      |
|---                                    |---     |---                                                                   |
| `POST /orders/checkout`               | 400    | Validation — show field errors                                       |
|                                       | 401    | Token expired — re-login                                             |
|                                       | 403    | Missing `order.create` permission — disable Pay button               |
|                                       | 409    | Idempotency replay with DIFFERENT body — show "checkout in progress" |
|                                       | 5xx    | Retry with SAME `X-Idempotency-Key`, exp backoff (max 3)             |
| `GET  /payment-intents/{id}`          | 404    | Intent missing — call `/refresh`                                     |
| `POST /payment-intents/{id}/refresh`  | 400    | Order isn't `online` — error toast                                   |
|                                       | 409    | Payment already terminal — re-fetch `/orders/{id}`                   |
|                                       | 502    | Gateway down — retry w/ backoff, then degrade to "QR unavailable, ask cashier" |
| Any                                   | 429    | Honour `Retry-After`, exponential backoff                            |

---

## 5. UX rules

- Disable the Pay button between click and 2xx from `/checkout`.
- Show a spinner with copy: *"Creating order…"* during checkout, *"Waiting for payment…"* during polling.
- When QR is shown, also display `amount`, `qr_close_by` countdown, and a "Cancel & switch to cash" affordance that calls `DELETE /orders/{order_id}`.
- On `payment_status == "completed"`: stop polling immediately, fetch `/orders/{id}`, render receipt with `id`, `total_amount`, `payment.razorpay_payment_id`, items.
- Do NOT trust client-side `total_amount` for the receipt — always render the server value.

---

## 6. What to deliver

1. A typed API client module (`api/orders.ts`, `api/paymentIntents.ts`) wrapping the 4 endpoints above. Strict TS types for `CheckoutIn`, `IntentOut`, `QrOut`.
2. A `useCheckout()` hook (or equivalent state-management primitive for your framework) that implements the state machine in §2, including the idempotency-key lifecycle.
3. A `<CheckoutQR />` component that renders `qr_image_url` (preferred) or generates from `qr_image_content` (UPI deep link) and shows the countdown.
4. A `<Receipt />` component that consumes `GET /orders/{id}`.
5. Unit tests for: idempotency-key reuse on retry, polling-stop on terminal payment state, refresh fallback when `razorpay.error` present, currency formatting.
6. No webhook handling on the client. No Razorpay JS SDK / Checkout.js — the backend uses Razorpay Orders API + QR codes; the QR image URL is all the client needs.

---

## 7. Hard constraints

- Never call gateway APIs directly from the frontend.
- Never bypass the idempotency key on `/checkout`.
- Never mutate orders after `payment_status == completed` (no edit/delete UI in that state).
- Do not use `payment_method = "online"` together with `order_type = "pos"` unless the QR is actually presented to the customer — it's the only path that creates a Razorpay intent.
- The `payment_method` aliases that map to online are: `online | razorpay | gateway | netbanking`. Pick one canonical value in your UI (recommend `"online"`).
