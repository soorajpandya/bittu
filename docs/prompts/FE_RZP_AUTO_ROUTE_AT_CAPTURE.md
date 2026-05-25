# Frontend prompt — Razorpay auto-route at capture (no FE wiring change required)

> Deployed: `e398d6a` on `ec2-13-206-196-252` (Mumbai), `/api/v1/health → 200 ok`.
> Companion to `FE_RZP_LINKED_ACCOUNT_CONTRACT.md` (onboarding) and
> `MERCHANT_FINANCE_FRONTEND_CONTRACT.md` (wallet / statement).

---

## TL;DR for the FE team

**Nothing in the request/response shape of the QR / payment-intent endpoints changed.** Keep calling them exactly as today. This document exists so you understand the new server behaviour and can update copy + polling assumptions accordingly.

What changed on the backend:

- When the customer pays a QR for a merchant whose linked account is **activated** (`effective_status == "activated"`), Razorpay now atomically credits the merchant's linked account with **95%** of the captured amount and Bittu's platform account with the remaining 5%, *at capture time*. No background worker, no manual step.
- For merchants who are **not** activated yet (`pending / submitted / under_review / needs_clarification / rejected / suspended`), the existing `assert_settlement_ready` gate already blocks new QR generation with HTTP `409` — so this case is unreachable on the happy path. Keep your existing 409 banner.
- The wallet & statement endpoints already project from the captured amount × 0.95, so the merchant's "available balance" appears the moment the QR is paid — no longer waiting on a delayed transfer to materialise.

---

## 1. Endpoints touched — contract diff

| Endpoint | Request | Response | Diff |
|---|---|---|---|
| `POST /api/v1/payment-intents` | unchanged | unchanged | none |
| `POST /api/v1/payments/{id}/intent` | unchanged | unchanged | none |
| `GET  /api/v1/payment-intents/{id}` | unchanged | unchanged | none |
| `GET  /api/v1/merchants/me/wallet` | unchanged | unchanged | none (already projects 95%) |
| `GET  /api/v1/merchants/me/statement` | unchanged | unchanged | none |

**FE action: zero code change required for the QR / payment flow itself.**

---

## 2. What you can stop doing (optional cleanups)

If your current implementation has any of the workarounds below, you can drop them. None of these are blockers — they just become dead code.

1. **"Funds will reach you within 24 hours" disclaimer on QR success screens** for activated merchants — funds are now in their Route ledger within seconds of capture. You can show "Settled to your linked bank account" instead (or whatever copy matches the next-day settlement cycle Razorpay runs).
2. **Manual "refresh wallet" hint after a payment** — the wallet's `pending` field updates immediately on `payment.captured` because it's projected from `rzp_payments × 0.95`, not from the (now-redundant) async transfer worker.
3. **Polling `GET /merchants/me/wallet` faster than the WebSocket push** — the `merchant_wallet_updated` WS event still fires on `payment.captured`; rely on that and you can drop any 5-second polling fallback.

---

## 3. What the FE MUST still do

### 3.1 Gate QR generation on `effective_status == "activated"`

This was already the spec, but it's worth re-stating because the new auto-route behaviour depends on it:

- Before showing the "Generate QR" button on the POS / customer-app, check `GET /api/v1/razorpay-route/linked-account → effective_status`.
- If it's anything other than `activated`, show the onboarding CTA from `FE_RZP_LINKED_ACCOUNT_CONTRACT.md §2` instead of a QR generator.
- If you skip this and call `POST /payment-intents` for a non-activated merchant, the backend returns:
  ```json
  HTTP 409
  { "detail": "merchant_not_settlement_ready: linked_account_status='created' product_status='under_review'" }
  ```
  Render the same onboarding CTA on this 409.

### 3.2 Handle the (rare) "transfer skipped — amount too small" case in copy

Razorpay's minimum per-transfer amount is `₹1.00` (100 paise). For QR amounts where `floor(amount × 0.95) < ₹1` (i.e. gross ≤ ₹1.05), the backend now logs a warning and creates the order **without** a transfer split — the capture lands on the platform account and is reconciled later.

- This is only relevant for test / nominal payments. Production POS amounts are never this small.
- The wallet still projects this capture at 95% (so the merchant sees the rupees), it just doesn't auto-settle to their bank in the same Razorpay cycle.
- **FE action: none.** Just don't be surprised if a `₹1` test payment shows up in `pending` for slightly longer.

### 3.3 Don't read `transfer_id` off the payment-intent response

The intent response still does not (and will not) include a `transfer_id` — transfers are created server-side by Razorpay at capture and arrive via the `transfer.processed` webhook. If you need to surface "settled vs pending" per payment, use the existing statement endpoint, which already merges payment + transfer rows.

---

## 4. Mental model for support / ops UI

If your build has an internal "merchant payment debug" view, here's the new flow worth visualising:

```
Customer scans QR
        │
        ▼
POST /v1/payments/{id}/capture    ← Razorpay-side
        │
        ▼
Razorpay splits the captured payment per the transfers[] in the order
        │
        ├── 95% → merchant's linked account (acc_xxx)
        └── 5%  → Bittu platform account
        │
        ▼
Razorpay fires `payment.captured` + `transfer.processed` webhooks
        │
        ├── payment.captured  → wallet `pending` reflects 95% instantly
        └── transfer.processed → `rzp_route_transfers` row + statement entry
        │
        ▼
Next Razorpay settlement cycle (T+1 for restaurants by default)
        │
        ▼
Funds land in the merchant's bank account on file
```

The two boxes the FE renders against (wallet "pending", statement "transfer") are now in sync from the very first second after capture — there's no async window where one is populated and the other isn't.

---

## 5. Acceptance checklist for the FE PR

- [ ] No code change to QR / payment-intent request bodies.
- [ ] (Optional) Copy update on the QR success screen for activated merchants.
- [ ] Onboarding CTA still rendered for HTTP 409 `merchant_not_settlement_ready` on `POST /payment-intents`.
- [ ] Wallet view does not re-poll faster than the `merchant_wallet_updated` WS event (it's already real-time).
- [ ] Support/debug view (if any) reflects the new "auto-split at capture" model — no more "transfer pending" intermediate state for activated merchants.

---

## 6. Backfill note (one-time, ops-only)

There are ~10 captures sitting on the platform account that were created **before** this deploy (i.e. orders without the new `transfers[]` array). They will be migrated by a one-off backend script (`_backfill_route_transfers.py`) that calls `POST /v1/payments/{id}/transfers` for each. Once that script runs, those payments will also appear as settled to the merchant — **no FE action needed**.
