# QR Dine-In — Frontend Implementation Guide

Complete instructions for the customer-facing QR table ordering app: **scan →
menu → cart → order → pay (post-paid) → settle**, plus the **session-security**
model (token invalidation on close) and **device-binding** (table PIN).

All endpoints are under base path **`/api/v1`**. The `/dinein/qr/*` endpoints
are **public** (no JWT) and authenticate with the `session_token` returned by
scan. **Use `/dinein/*` for everything** — do not mix the legacy `/tables/*`
endpoints.

---

## 0. Client responsibilities

- **`device_id`** — generate once and **persist in localStorage**. Reuse it on
  every call and across refreshes. This is what keeps the session seamless and
  makes device-binding work. Do **not** regenerate it per page load.
- **`session_token`** — store it after scan/join; send it on every subsequent
  call. Treat it as the session credential.
- **`request_id`** — for `cart/add`, `cart/update`, `place-order`, `pay`, send a
  fresh UUID per user action for idempotency (safe retries).

---

## 1. Scan — `POST /api/v1/dinein/qr/scan`

The QR encodes `restaurant_id` + `table_id`. On open, send those plus your
persisted `device_id` and (if you have one) the saved `session_token`.

```jsonc
// request
{ "restaurant_id": "<uuid>", "table_id": "<uuid>", "device_id": "<persisted>", "session_token": "<saved or null>" }
```

**Three possible responses — you MUST handle all three:**

**(a) Normal — you own/rejoin the session** (`is_new` true or a known device):
```jsonc
{ "session_id": "...", "session_token": "...", "table": {...}, "restaurant": {...},
  "is_new": true, "join_pin": "4821", "active_order_id": null }
```
→ Save `session_token`. If `join_pin` is present, show it subtly ("Table PIN
4821 — share to let friends order from this table").

**(b) Requires join — a different device already owns this table's session:**
```jsonc
{ "requires_join": true, "session_id": "...", "table": {...}, "restaurant": {...},
  "is_new": false, "message": "This table already has an active session. Enter the table PIN to join." }
```
→ There is **no `session_token`**. Prompt the user for the 4-digit table PIN and
call **`/qr/join`** (step 1b). Show the `message`.

**(c) Restore — you sent a valid saved token** → same shape as (a) with
`is_new: false`. Seamless reconnect.

> **Security behavior:** After the bill is paid and the session closes, the
> server rotates the token. A saved token or saved URL opened later will get
> either a "session not found / closed" error (token no longer valid) or, on
> re-scan, a **brand-new empty session** — the old paid bill can never be
> reopened or increased.

## 1b. Join — `POST /api/v1/dinein/qr/join`

Only when scan returned `requires_join: true`.

```jsonc
// request
{ "restaurant_id": "<uuid>", "table_id": "<uuid>", "device_id": "<persisted>", "join_pin": "4821" }
// response
{ "session_id": "...", "session_token": "...", "is_new": false, "active_order_id": "...", "message": "Joined the table session." }
```

Wrong PIN → `400 { "detail": "Invalid table PIN" }`. Let the user retry. Save
the returned `session_token` on success.

---

## 2. Menu — `GET /api/v1/dinein/qr/menu?restaurant_id=<uuid>`

Returns `categories`, `items`, `variants`, `addons`, `extras`,
`modifierGroups`, `combos`, `comboItems`.

> **CRITICAL — `item_id` must be the integer `Item_ID` from a menu *item*.**
> This is the #1 cause of the "add button does nothing" bug. Do **not** pass a
> category id, a variant UUID, or a **combo** UUID as `item_id`. Combos are not
> addable via `cart/add` yet (they carry UUIDs) — add their component items.

---

## 3. Cart

Add — `POST /api/v1/dinein/qr/cart/add`
```jsonc
{ "session_token": "...", "item_id": 1234, "quantity": 1, "variant_id": null,
  "addons": [], "extras": [], "notes": "", "device_id": "<persisted>", "request_id": "<uuid>" }
// -> { "cart_item_id": "...", "item_name": "...", "quantity": 1, "total_price": 180, "status": "added" }
```

> **After a 200 from `cart/add`, re-render the cart.** The backend returns 200
> and stores the item; if your UI doesn't update, it's because it isn't
> re-fetching. Either apply the returned fields optimistically **or** call
> `GET /qr/cart?session_token=...` and repaint. (This is the current
> "button not functional" symptom — the request succeeds server-side.)

Other ops:
- `POST /qr/cart/update` `{ session_token, cart_item_id, quantity?, addons?, extras?, request_id? }`
- `POST /qr/cart/remove` `{ session_token, cart_item_id }`
- `POST /qr/cart/clear?session_token=...`
- `GET  /qr/cart?session_token=...`

---

## 4. Place order (KOT) — `POST /api/v1/dinein/qr/place-order`

```jsonc
{ "session_token": "...", "notes": "", "customer_name": "", "customer_phone": "", "request_id": "<uuid>" }
```

Sends the current cart to the kitchen immediately (post-paid). **Adding more
items later and calling place-order again appends to the same order** (KOT #2,
#3…). Track with `GET /qr/order-status?session_token=...`.

---

## 5. Bill — `GET /api/v1/dinein/qr/bill?session_token=<string>`

```jsonc
{ "orders": [...], "grouped_items": [...], "subtotal": 500, "tax": 40, "discount": 0,
  "grand_total": 540, "paid_total": 0, "remaining_amount": 540, "payments": [] }
```

Poll every ~5s while the table is open to show the running total.

---

## 6. Pay (post-paid) — `POST /api/v1/dinein/qr/pay`

```jsonc
// request
{ "session_token": "...", "customer_name": "", "customer_phone": "" }
```

Charges the **remaining** bill. Branch on `mode`:

**(a) `mode: "integrated"`** (merchant on an integrated-payments plan):
```jsonc
{ "mode": "integrated", "amount": 540, "amount_paise": 54000, "currency": "INR",
  "razorpay_order_id": "order_XXX", "qr_id": "qr_XXX",
  "qr_image_url": "https://...png", "qr_image_content": "upi://pay?pa=...&am=540.00&cu=INR",
  "qr_close_by": "...", "settlement": "automatic", "poll_status_after_ms": 3000,
  "message": "Scan the UPI QR or open your UPI app to pay. Payment confirms automatically." }
```
→ Render `qr_image_url` **or** deep-link `qr_image_content` to open the UPI app.
Then poll `/qr/payment-status`. Confirmation is **automatic** (webhook).

**(b) `mode: "own_upi"`** (merchant using their own UPI):
```jsonc
{ "mode": "own_upi", "amount": 540, "amount_paise": 54000, "currency": "INR",
  "upi_intent": "upi://pay?pa=<merchant-vpa>&am=540.00&cu=INR&pn=...",
  "qr_image": "data:image/png;base64,...", "settlement": "manual",
  "message": "Pay to the restaurant's UPI, then ask staff to confirm your payment." }
```
→ Render `qr_image` / deep-link `upi_intent`. Show a "waiting for staff to
confirm" state — settlement is **manual** (staff confirm at POS). Keep polling
`/qr/payment-status`.

Common errors: `This session has nothing left to pay`, `Place an order before
paying`, `Online payments are not enabled for this restaurant yet`, `This
restaurant has not set up its UPI payment QR yet`.

## 7. Payment status — `GET /api/v1/dinein/qr/payment-status?session_token=<string>`

```jsonc
{ "session_id": "...", "grand_total": 540, "paid_amount": 540, "remaining_amount": 0, "is_paid": true, "payments": [...] }
```

Poll every ~3s after initiating payment. When `is_paid == true`, show the
success screen. The server auto-vacates the table when fully paid. After this,
the `session_token` is dead — clear it locally.

---

## Screen / state flow

```
Scan ──▶ requires_join? ──yes──▶ PIN prompt ──▶ /qr/join ──┐
   │                                                        │
   └── no (token) ─────────────────────────────────────────┤
                                                            ▼
                                Menu ──▶ Cart(add/update) ──▶ Place order ──▶ (repeat)
                                                            ▼
                                        Bill ──▶ Pay (integrated | own_upi) ──▶ poll status ──▶ Paid ──▶ clear token
```

## State machine the FE must implement

| State | Enter when | Actions available |
|-------|-----------|-------------------|
| `SCANNING` | app opened via QR | call `/qr/scan` |
| `NEEDS_PIN` | scan → `requires_join` | show PIN input → `/qr/join` |
| `ORDERING` | have `session_token` | menu, cart, place-order |
| `BILL` | user taps "View bill / Pay" | `/qr/bill`, `/qr/pay` |
| `AWAITING_PAYMENT` | after `/qr/pay` | poll `/qr/payment-status` |
| `PAID` | `is_paid == true` | success screen; **clear `session_token`** |
| `SESSION_DEAD` | any call returns "session not found/closed" | clear token; offer re-scan |

## Error handling rules

- **401/404 "session" errors mid-flow** → the session was closed/expired or the
  token was rotated after payment. Drop to `SESSION_DEAD`, clear `session_token`,
  and prompt a fresh scan. **Never** silently reuse an old token.
- **`requires_join`** → never treat as an error; route to `NEEDS_PIN`.
- **`cart/add` 200 but UI stale** → you forgot to re-render; re-fetch `/qr/cart`.
- **Persist `device_id`** — if it changes, a returning customer will be treated
  as a new device and asked for the PIN.

## Security notes (why the flow behaves this way)

- The QR carries only public IDs; the **`session_token` is the credential**.
- On payment/close the server **rotates the token** and closes the session, so a
  **saved URL/token opened later is useless** — it cannot reopen or inflate the
  paid bill.
- A **new device** joining a live session must supply the **table PIN**
  (`/qr/join`) — a stranger or a saved URL on another phone can't piggyback.
- The **original device stays seamless** throughout the meal via its persisted
  `device_id` + `session_token`; the PIN only gates *new* devices.
