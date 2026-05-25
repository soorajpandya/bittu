# Frontend prompt — Merchant Ledger balance bifurcation

> Deployed: `e0a0238` on `ec2-13-206-196-252` (Mumbai), `/api/v1/health → 200 ok`.
> Affects only one endpoint: `GET /api/v1/merchant-ledger/balance`.
> No breaking changes — pure additive payload.

---

## Why this prompt exists

Today the Merchant Ledger screen shows a single number (e.g. *"Balance ₹9.98"*) and the merchant cannot tell whether that money has been settled to their bank, is still owed, has been refunded, or what Bittu's commission was. The backend now returns the full breakdown on the same endpoint — the FE just needs to render it.

---

## 1. Endpoint contract diff

`GET /api/v1/merchant-ledger/balance` — unchanged request, **additive** response.

### Before

```json
{
  "merchant_id":        "751c6d1d-…",
  "wallet_status":      "active",
  "current_balance":    9.98,
  "pending_settlement": 9.98,
  "available_balance":  1.00,
  "settled_amount":     0.00,
  "refunded_amount":    0.00,
  "currency":           "INR"
}
```

### After (this deploy)

```json
{
  "merchant_id":           "751c6d1d-…",
  "wallet_status":         "active",
  "currency":              "INR",

  // ── back-compat (unchanged meaning) ─────────────────────────────
  "current_balance":       9.98,   // == pending_settlement; keep using this as the headline
  "pending_settlement":    9.98,

  // ── NEW bifurcation fields (all in rupees, lifetime) ────────────
  "gross_sales":           10.50,  // total customers paid (∑ captured payments)
  "platform_commission":    0.52,  // Bittu's 5% commission (gross − merchant_earned)
  "merchant_earned":        9.98,  // merchant's 95% share = gross × 0.95
  "transferred_to_linked":  1.00,  // ∑ Route transfers that have left the platform account
  "settled_amount":         0.00,  // ∑ already paid into the merchant's bank
  "refunded_amount":        0.00,
  "available_balance":      1.00   // sitting on linked account, not yet bank-settled
}
```

### Invariant the FE can rely on (and assert in dev builds)

```
merchant_earned == pending_settlement + settled_amount + refunded_amount
```

If that ever fails, surface a soft warning + retry — it indicates the projection lagged a webhook.

---

## 2. UI to render

Replace the single "Balance" tile with a 4-line card:

```
┌────────────────────────────────────────────────┐
│  Available to settle                  ₹ 9.98   │ ← current_balance (headline, big)
│  ────────────────────────────────────────────  │
│  Total earned                         ₹ 9.98   │ ← merchant_earned
│  Platform commission (5 %)            ₹ 0.52   │ ← platform_commission
│  Settled to your bank                 ₹ 0.00   │ ← settled_amount
│  Pending settlement                   ₹ 9.98   │ ← pending_settlement
│  (Refunds issued                      ₹ 0.00)  │ ← refunded_amount — hide row if 0
└────────────────────────────────────────────────┘
```

- **Headline** stays the same string as today (`current_balance`) so nothing breaks visually.
- Each sub-line should be a `key/value` row, right-aligned amount, secondary-text colour for the labels.
- Show `Refunds issued` only when `refunded_amount > 0` to avoid clutter.
- `gross_sales` is optional — keep it as a tooltip on the "Total earned" row: *"From ₹{gross_sales} in sales"*.

### Optional "ops / debug" reveal

Behind a long-press or developer toggle (not visible to the merchant), also show:

- **Transferred to linked account**: `transferred_to_linked` — sanity check that auto-route is firing.
- **Available on Razorpay**: `available_balance` — money already on the linked account but not yet swept to the merchant's bank.

These are diagnostic-only; merchant copy should never lead with them.

---

## 3. Copy guidance

| Field | Label (en-IN) | Tooltip |
|---|---|---|
| `current_balance`     | Available to settle    | The amount that will land in your bank in the next Razorpay settlement cycle. |
| `merchant_earned`     | Total earned           | Your 95 % share of all online payments so far. Bittu keeps 5 % as a platform fee. |
| `platform_commission` | Platform commission    | Flat 5 % Bittu fee on every online payment. Cash sales are commission-free. |
| `settled_amount`      | Settled to your bank   | Already paid into your linked bank account by Razorpay. |
| `pending_settlement`  | Pending settlement     | Earned but not yet in your bank — usually credits within one business day. |
| `refunded_amount`     | Refunds issued         | Total you've refunded back to customers. |

Do **not** show "Available on Razorpay" or "Transferred to linked account" to merchants — those phrases will confuse them. Keep them in the ops view only.

---

## 4. Refresh / realtime

- The wallet WS push (`merchant_wallet_updated`) already fires on capture, transfer, settlement and refund — re-call `GET /merchant-ledger/balance` on every receipt of that event and re-render the card.
- The endpoint is cached server-side for 30 s, so no need to hammer it on every screen focus; once per `merchant_wallet_updated` is enough.

---

## 5. Acceptance checklist

- [ ] Card renders 4–5 rows under the headline (Total earned, Commission, Settled, Pending, optional Refunds).
- [ ] Headline still uses `current_balance` (no behaviour change).
- [ ] `Refunds issued` row is hidden when `refunded_amount == 0`.
- [ ] Tooltips wired with the copy above.
- [ ] `transferred_to_linked` + `available_balance` are NOT shown in the merchant-facing UI.
- [ ] Dev-only assertion: `merchant_earned ≈ pending_settlement + settled_amount + refunded_amount` (within ₹0.01).
- [ ] Card re-fetches on `merchant_wallet_updated` WS event.

---

## 6. Why the FE may see `transferred_to_linked < merchant_earned`

Two reasons, both expected:

1. **Pre-deploy captures**: payments captured before commit `e398d6a` were created without `transfers[]`, so no transfer row exists for them yet. A one-off backend script will backfill these.
2. **Sub-₹1 splits**: any payment where `floor(gross × 0.95) < ₹1` skips the auto-split (Razorpay rejects transfers below ₹1). These are reconciled later.

In both cases the merchant is still credited via `pending_settlement` / `current_balance` — the gap only matters for the ops view.
