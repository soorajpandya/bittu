# Merchant Finance — Frontend Contract (v2, Razorpay-backed)

> **Source of truth**: live Razorpay REST API, scoped per merchant via the
> `X-Razorpay-Account` linked-account header. There is **no** Supabase wallet
> or ledger table behind these endpoints anymore — every read is a live
> Razorpay call (cached 30–60 s).
>
> **Commission model**: Bittu retains a flat **5%** of every captured payment.
> The split is applied at projection time:
>
> ```
> merchant_amount   = razorpay_settlement.amount   (what hit merchant's bank)
> gross_amount      = merchant_amount / 0.95       (back-computed)
> commission_amount = gross_amount - merchant_amount
> ```
>
> All monetary values are **rupees as JSON numbers** (2dp). Currency is INR
> unless stated. All timestamps are ISO 8601 in UTC.

---

## Auth

All endpoints require the standard `Authorization: Bearer <jwt>` header.
The merchant is resolved from `restaurant_id` in the JWT context — the FE
does **not** pass a merchant id.

| Permission              | Used by                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `bank_recon.read`       | wallet snapshot, transactions, settlements list / detail / timeline  |
| `statements.export`     | CSV export                                                           |
| `merchant_ledger.read`  | ledger balance, entries list, single entry                           |
| `merchant_ledger.admin` | consistency-check, manual-adjustment (now 410)                       |

---

## Error shape

```json
{ "detail": "Human-readable message" }
```

| Status | Meaning                                                              |
| ------ | -------------------------------------------------------------------- |
| 400    | Validation error                                                     |
| 401    | Missing / invalid JWT                                                |
| 403    | Missing required permission                                          |
| 404    | Merchant has no linked Razorpay Route account, or resource not found |
| 410    | Endpoint deprecated (manual-adjustment, settlement advance)          |
| 502    | Razorpay upstream error                                              |

> **404 onboarding hint**: when a merchant hasn't completed Route
> onboarding, every endpoint here returns
> `404 "No Razorpay Route account is linked to this merchant. Complete
> onboarding via POST /api/v1/razorpay-route/linked-account/onboard."`
> — FE should detect this and route the user to the onboarding flow.

---

# 1. Merchant Wallet (`/api/v1/merchant-wallet`)

## 1.1 `GET /api/v1/merchant-wallet`

Wallet snapshot with Bittu commission applied.

**Query**

| Param       | Type   | Default | Notes                                  |
| ----------- | ------ | ------- | -------------------------------------- |
| `from_date` | `date` | —       | Inclusive UTC. Omit for lifetime view. |
| `to_date`   | `date` | —       | Inclusive UTC.                         |

**Response 200**

```json
{
  "merchant_id": "11111111-1111-1111-1111-111111111111",
  "gross_sales": 12450.00,
  "settled_amount": 9500.00,
  "platform_commission": 622.50,
  "pending_settlement": 2327.50,
  "refunds": 150.00,
  "available_balance": 9350.00,
  "transaction_count": 87,
  "currency": "INR",
  "window": { "from": "2026-04-01", "to": "2026-04-30" }
}
```

Field semantics:

| Field                 | Meaning                                                                          |
| --------------------- | -------------------------------------------------------------------------------- |
| `gross_sales`         | Sum of all captured payments (customer-facing total).                            |
| `settled_amount`      | Sum of `processed` settlements (merchant's share already paid out).              |
| `platform_commission` | `gross_sales × 5%` — Bittu's cut on the window.                                  |
| `pending_settlement`  | `gross_sales × 95% − settled_amount`, clamped ≥ 0.                               |
| `refunds`             | Sum of refunds in `processed / pending / created / initiated`.                   |
| `available_balance`   | `settled_amount − refunds`, clamped ≥ 0. Money already at the merchant's bank.   |
| `transaction_count`   | Number of captured payments in the window.                                       |

---

## 1.2 `GET /api/v1/merchant-wallet/transactions`

Captured payments, filterable.

**Query**

| Param            | Type     | Notes                                                  |
| ---------------- | -------- | ------------------------------------------------------ |
| `payment_method` | `string` | `upi` / `card` / `netbanking` / `wallet` …             |
| `min_amount`     | `float`  | Rupees, ≥ 0.                                           |
| `max_amount`     | `float`  | Rupees, ≥ 0.                                           |
| `search`         | `string` | Matches payment_id / order_id / email / phone / vpa.   |
| `from_date`      | `date`   | UTC inclusive.                                         |
| `to_date`        | `date`   | UTC inclusive.                                         |
| `limit`          | `int`    | 1–500, default 50.                                     |
| `offset`         | `int`    | ≥ 0, default 0.                                        |

**Response 200**

```json
{
  "items": [
    {
      "transaction_id": "pay_NXyZ12345AbCdEf",
      "payment_id":     "pay_NXyZ12345AbCdEf",
      "order_id":       "order_NXyA98765QwErTy",
      "amount":            499.00,
      "gross_amount":      499.00,
      "merchant_amount":   474.05,
      "commission_amount":  24.95,
      "currency": "INR",
      "status": "captured",
      "payment_method": "upi",
      "method_detail": {
        "vpa": "raju@okhdfcbank",
        "bank": null, "wallet": null, "card_id": null
      },
      "customer_email":   "raju@example.com",
      "customer_contact": "+919876543210",
      "captured": true,
      "captured_at": "2026-04-21T11:32:18+00:00",
      "created_at":  "2026-04-21T11:32:11+00:00",
      "fee": 11.78,
      "tax":  1.80,
      "international": false,
      "description": "Order #4521",
      "notes": { "table": "T7" },
      "error_code": null,
      "error_description": null,
      "source": "razorpay"
    }
  ],
  "total": 87,
  "limit": 50,
  "offset": 0,
  "has_more": true
}
```

> `fee` / `tax` are Razorpay's own gateway fee + GST on it (informational
> only — independent of Bittu's 5%).

---

## 1.3 `GET /api/v1/merchant-wallet/settlements`

**Query**

| Param       | Type     | Notes                                       |
| ----------- | -------- | ------------------------------------------- |
| `status`    | `string` | `created` / `processing` / `processed` / `failed` |
| `from_date` | `date`   | UTC inclusive.                              |
| `to_date`   | `date`   | UTC inclusive.                              |
| `limit`     | `int`    | 1–500, default 50.                          |
| `offset`    | `int`    | ≥ 0, default 0.                             |

**Response 200**

```json
{
  "items": [
    {
      "settlement_id": "setl_NXyB44455ZzZzZz",
      "merchant_id":   "11111111-1111-1111-1111-111111111111",
      "gross_amount":     10000.00,
      "merchant_amount":   9500.00,
      "commission_amount":  500.00,
      "fees":   0.00,
      "tax":    0.00,
      "utr":    "HDFC0000000123456",
      "status": "processed",
      "created_at": "2026-04-22T05:00:00+00:00",
      "currency": "INR"
    }
  ],
  "total": 12,
  "limit": 50,
  "offset": 0,
  "has_more": false
}
```

---

## 1.4 `GET /api/v1/merchant-wallet/settlements/{settlement_id}`

Settlement detail. Best-effort recon attachment: `payments[]` is the
per-payment breakdown for that settlement (may be empty if recon is
not yet available for that month).

**Response 200**

```json
{
  "settlement_id": "setl_NXyB44455ZzZzZz",
  "merchant_id":   "11111111-1111-1111-1111-111111111111",
  "gross_amount":     10000.00,
  "merchant_amount":   9500.00,
  "commission_amount":  500.00,
  "fees": 0.00, "tax": 0.00,
  "utr": "HDFC0000000123456",
  "status": "processed",
  "created_at": "2026-04-22T05:00:00+00:00",
  "currency": "INR",
  "payments": [
    {
      "payment_id": "pay_NXyZ12345AbCdEf",
      "type": "payment",
      "amount":  499.00,
      "fee":      11.78,
      "tax":       1.80,
      "credit":  499.00,
      "debit":     0.00,
      "method":  "upi",
      "utr":     "HDFC0000000123456",
      "created_at": "2026-04-21T11:32:18+00:00"
    }
  ]
}
```

---

## 1.5 `GET /api/v1/merchant-wallet/settlements/{settlement_id}/timeline`

Synthetic timeline derived from Razorpay's current status. Razorpay does
not expose per-state transition timestamps; every state up to the current
one is marked `completed: true` anchored on `created_at`, future states
are `pending: true`.

**Response 200**

```json
{
  "settlement_id": "setl_NXyB44455ZzZzZz",
  "current_status": "processed",
  "utr": "HDFC0000000123456",
  "events": [
    { "status": "created",    "label": "Created",    "completed": true,  "pending": false, "at": "2026-04-22T05:00:00+00:00" },
    { "status": "processing", "label": "Processing", "completed": true,  "pending": false, "at": "2026-04-22T05:00:00+00:00" },
    { "status": "processed",  "label": "Processed",  "completed": true,  "pending": false, "at": "2026-04-22T05:00:00+00:00" },
    { "status": "failed",     "label": "Failed",     "completed": false, "pending": true,  "at": null }
  ],
  "source": "razorpay"
}
```

> When `current_status === "failed"`, the `processed` row carries
> `completed=false, pending=true` and `failed` carries `completed=true`.

---

## 1.6 `GET /api/v1/merchant-wallet/export`

CSV download of the reconciliation for a date window. Streams a
`text/csv` body with a `Content-Disposition: attachment` header. Defaults
to the last 30 days.

**Query**

| Param       | Type   | Default     |
| ----------- | ------ | ----------- |
| `from_date` | `date` | today − 30d |
| `to_date`   | `date` | today       |

**Response 200** — `text/csv`

```
Date,Payment ID,Settlement ID,Gross Amount,Merchant Amount,Commission,Refund,Status
2026-04-21T11:32:18+00:00,pay_NXyZ12345AbCdEf,setl_NXyB44455ZzZzZz,499.00,474.05,24.95,0.00,payment
2026-04-22T05:00:00+00:00,,setl_NXyB44455ZzZzZz,10000.00,9500.00,500.00,0.00,processed
```

FE handling: use the browser's native download (e.g. `window.location.assign`
on the absolute URL with auth via cookie, or `fetch` + `Blob` + anchor click
when using bearer tokens). Filename comes from the response header:
`statement_<merchant_id>_<from>_<to>.csv`.

---

# 2. Merchant Ledger (`/api/v1/merchant-ledger`)

The ledger is a **synthetic projection**:

| Razorpay source     | Ledger entry                                |
| ------------------- | ------------------------------------------- |
| captured payment    | `CREDIT` (`source: "payment"`)              |
| 5% on each payment  | `DEBIT`  (`source: "commission"`)           |
| processed settlement| `DEBIT`  (`source: "settlement"`)           |
| refund              | `DEBIT`  (`source: "refund"`)               |

`entry_id` is **deterministic** and **URL-safe**:
`pay:<rzp_payment_id>`, `com:<rzp_payment_id>`, `setl:<rzp_settlement_id>`,
`ref:<rzp_refund_id>`.

`balance_after` is the running balance computed in chronological order;
the API returns entries newest-first but the `balance_after` value is
preserved from the ascending stream so it remains correct.

## 2.1 `GET /api/v1/merchant-ledger/balance`

**Response 200**

```json
{
  "merchant_id": "11111111-1111-1111-1111-111111111111",
  "current_balance": 2477.50,
  "pending_settlement": 2327.50,
  "settled_amount": 9500.00,
  "refunded_amount": 150.00,
  "currency": "INR"
}
```

| Field                | Meaning                                                                       |
| -------------------- | ----------------------------------------------------------------------------- |
| `current_balance`    | `gross × 95% − refunds`, clamped ≥ 0. The merchant's net claim on captured volume. |
| `pending_settlement` | `gross × 95% − settled`, clamped ≥ 0.                                         |
| `settled_amount`     | Sum of processed settlements.                                                 |
| `refunded_amount`    | Sum of refunds in non-terminal-failed state.                                  |

---

## 2.2 `GET /api/v1/merchant-ledger/entries`

**Query**

| Param        | Type     | Notes                                            |
| ------------ | -------- | ------------------------------------------------ |
| `entry_type` | `string` | `CREDIT` or `DEBIT`                              |
| `source`     | `string` | `payment` / `settlement` / `commission` / `refund` |
| `from_date`  | `date`   | UTC inclusive                                    |
| `to_date`    | `date`   | UTC inclusive                                    |
| `limit`      | `int`    | 1–200, default 50                                |
| `offset`     | `int`    | ≥ 0, default 0                                   |

**Response 200**

```json
{
  "items": [
    {
      "entry_id": "setl:setl_NXyB44455ZzZzZz",
      "type": "DEBIT",
      "source": "settlement",
      "amount": 9500.00,
      "reference": "setl_NXyB44455ZzZzZz",
      "utr": "HDFC0000000123456",
      "at_ts": 1745298000,
      "at": "2026-04-22T05:00:00+00:00",
      "description": "Settled to bank (HDFC0000000123456)",
      "currency": "INR",
      "balance_after": 0.00
    },
    {
      "entry_id": "com:pay_NXyZ12345AbCdEf",
      "type": "DEBIT",
      "source": "commission",
      "amount": 24.95,
      "reference": "pay_NXyZ12345AbCdEf",
      "order_id": "order_NXyA98765QwErTy",
      "at_ts": 1745234039,
      "at": "2026-04-21T11:32:18+00:00",
      "description": "Bittu platform commission (5%)",
      "currency": "INR",
      "balance_after": 9500.00
    },
    {
      "entry_id": "pay:pay_NXyZ12345AbCdEf",
      "type": "CREDIT",
      "source": "payment",
      "amount": 499.00,
      "reference": "pay_NXyZ12345AbCdEf",
      "order_id": "order_NXyA98765QwErTy",
      "at_ts": 1745234038,
      "at": "2026-04-21T11:32:18+00:00",
      "description": "Captured payment pay_NXyZ12345AbCdEf",
      "currency": "INR",
      "balance_after": 9524.95
    }
  ],
  "total": 264,
  "limit": 50,
  "offset": 0,
  "has_more": true
}
```

Optional fields (only present when relevant): `order_id`, `payment_id`,
`utr`.

---

## 2.3 `GET /api/v1/merchant-ledger/entries/{entry_id}`

`entry_id` must be one of `pay:` / `com:` / `setl:` / `ref:` prefixed.

**Response 200** — same shape as a single item in `entries`.

`404` if the entry isn't found in the current Razorpay state (e.g. the
underlying payment was deleted or never existed on this merchant's
linked account).

---

## 2.4 `GET` / `POST /api/v1/merchant-ledger/consistency-check`

Re-derives the ledger from live Razorpay data and verifies that
`sum(CREDIT) − sum(DEBIT) == current_balance`. Requires
`merchant_ledger.admin`.

**Response 200**

```json
{
  "merchant_id": "11111111-1111-1111-1111-111111111111",
  "consistent": true,
  "derived_balance": 2477.50,
  "live_balance":    2477.50,
  "delta": 0.00,
  "total_credit": 12450.00,
  "total_debit":   9972.50,
  "entry_count": 264,
  "source": "razorpay"
}
```

Surface a banner when `consistent === false` (rare; usually means a
Razorpay state change between the two reads).

---

## 2.5 `POST /api/v1/merchant-ledger/manual-adjustment` — **410 Gone**

Always returns:

```json
{
  "detail": "Manual ledger adjustments are disabled. The merchant ledger is now derived live from Razorpay; mutate the underlying Razorpay entity instead."
}
```

FE should hide the "Manual adjustment" UI for the merchant role. Admin
back-office adjustments must be performed via refund/credit-note flows on
Razorpay directly.

---

# 3. Statements (`/api/v1/statements`)

## 3.1 `POST /api/v1/statements/settlements/{settlement_id}/advance` — **410 Gone**

Settlement lifecycle is now owned by Razorpay. Always returns:

```json
{
  "detail": "Manual settlement advancement is disabled. Settlement state is now owned by Razorpay; use GET /merchant-wallet/settlements/<id>/timeline to inspect the live lifecycle."
}
```

FE: remove the "advance settlement" admin action. Use the timeline
endpoint above to render lifecycle state.

> The other `/api/v1/statements/*` endpoints (summary, transactions,
> settlements list, pending, timeline, fee calculator, payment enqueue,
> CSV export) are still mounted for back-compat. **Migrate to the
> `/api/v1/merchant-wallet/*` equivalents** — those are the new contract.

---

# 4. Migration cheat-sheet

| Old route                                                    | New route                                                       |
| ------------------------------------------------------------ | --------------------------------------------------------------- |
| `GET /api/v1/statements/summary`                             | `GET /api/v1/merchant-wallet`                                   |
| `GET /api/v1/statements/transactions`                        | `GET /api/v1/merchant-wallet/transactions`                      |
| `GET /api/v1/statements/settlements`                         | `GET /api/v1/merchant-wallet/settlements`                       |
| `GET /api/v1/statements/settlements/{id}`                    | `GET /api/v1/merchant-wallet/settlements/{id}`                  |
| `GET /api/v1/statements/settlements/{id}/timeline`           | `GET /api/v1/merchant-wallet/settlements/{id}/timeline`         |
| `GET /api/v1/statements/export`                              | `GET /api/v1/merchant-wallet/export`                            |
| `POST /api/v1/statements/settlements/{id}/advance`           | **410** — use the timeline endpoint to read state.              |
| `GET /api/v1/merchant-wallet/fee-quote`                      | *removed* — commission is a flat 5%, no quote needed.           |
| `GET /api/v1/merchant-wallet/daily-closing`                  | *removed* — call `GET /merchant-wallet?from_date=…&to_date=…`.  |
| `GET /api/v1/merchant-wallet/platform-revenue`               | *removed* — derive from `platform_commission` in the snapshot.  |
| `GET /api/v1/merchant-wallet/gst-on-fee`                     | *removed* — GST on commission must be handled offline.          |
| `POST /api/v1/merchant-ledger/manual-adjustment`             | **410** — adjust the underlying Razorpay entity.                |

---

# 5. Operational notes for FE

1. **First-call latency**: live Razorpay reads can take 800–2500 ms. The
   server caches responses for 30–60 s per param-set, so subsequent
   loads are fast. Show skeletons, not spinners, on first paint.
2. **Window guidance**: omit `from_date` / `to_date` only for "lifetime"
   tiles. For everything else pass an explicit window — wallet snapshot
   walks up to 5 000 payments per call.
3. **Pagination**: server-side total is the *filtered* count (after
   `payment_method` / `search` / `status`). Use `has_more` rather than
   computing `offset + limit < total` yourself.
4. **No realtime push**: there is no websocket channel for these views
   in v2. Re-fetch on a 60 s cadence (or on tab focus) for "live"
   feel.
5. **Currency**: always INR. Don't render a currency picker.
6. **Onboarding gate**: a `404` with the "No Razorpay Route account is
   linked …" detail is the canonical signal that the merchant must
   complete `POST /api/v1/razorpay-route/linked-account/onboard` first.
