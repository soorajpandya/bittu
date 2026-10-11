# Route payment corrections

Confirmed business rule: customer ₹100 → merchant ₹95. The ₹5 deduction covers all Razorpay charges and Bittu's residual. If actual gateway charges are ₹1.65, Bittu's residual is ₹3.35. Gateway cost estimates never change the merchant entitlement. A cost above ₹5 is a platform loss, not another merchant deduction. Integer paise rounding is applied once to the total deduction.

## Changed behavior

- New orders no longer create order-level transfers. The captured-payment handler owns the split; QR captures resolve their saved local order before crediting it.
- Captures validate local merchant, amount, currency and payment identity. An atomic claim prevents two gateway payments from funding the same canonical payment concurrently.
- Route service validates payment ownership and the 95% amount. It serializes per payment and queries existing gateway transfers before creating one. Historical transfers are preserved, not retroactively repriced. Existing order-owned splits wait for reconciliation.
- Transfer/reversal intent is persisted before sending HTTP. Successful results are reusable. Unknown responses block another mutation until reconciled. Unsupported generic idempotency headers and blind POST retries were removed.
- Recovery of a timed-out transfer now closes its durable unknown operation only after the gateway total matches the saved merchant entitlement. A mismatched amount remains unresolved. Failed or invalid creation responses cannot complete a capture or trigger an automatic second transfer.
- Refund reversal validates every active transfer recipient before sending any reversal. Transfer failure checks give the documented `transfer_status` field precedence over the legacy `status` field.
- Missing/inactive Route onboarding blocks new payment intents. Order intents whose merchant share is below ₹1 are rejected instead of silently accepted without a split.
- Critical capture ledger, escrow and Route errors fail the webhook. Failed events can retry; abandoned claims can be reclaimed after ten minutes. Claim tokens prevent an old worker from overwriting a newer attempt.
- Partial refunds leave the payment completed. Processed refund state cannot regress when an older event arrives. Reversals use cumulative original transfer proportions, including rounding recovery at a full refund. A later refund waits if an earlier reversal is still unconfirmed.
- Refund creation is serialized per merchant/payment and deduplicated by client request key. Gateway timeouts remain processing, reserving the refund balance. Gateway webhook notes reconnect the local refund after a lost HTTP response. Online refund status cannot be manually set through the merchant transition endpoint.
- Raw Route APIs and manual transfer reversals require platform-admin access. Transfer synchronization verifies the linked-account recipient.
- Wallet/ledger summaries use saved entitlements or historical transfers; missing historical allocations are flagged. Refund debits use the merchant's original proportion. Statement rows aggregate transfers and recover refund rounding. `platform_commission` remains a compatibility name for the combined deduction; the explicit `total_deduction` field was added. Do not label this entire amount as Bittu revenue.
- Settlement polling uses product activation and reads all pages. Reconciliation compares summed transfers to the saved paise entitlement, replacing the previous 20% tolerance for new allocations.
- Tenant database settings now survive autocommit statements and reset on pool release.

## Deployment and client requirements

1. Apply `migrations/086_route_payment_safety.sql` before deploying the changed application. It has only been exercised in an isolated test database, not applied to your live database.
2. Update refund clients to send `X-Idempotency-Key` (1–200 characters). Reuse the same key and request body for retries; use a new key for an intentionally different refund. Missing keys return HTTP 422. Keys with changed request bodies are rejected.
3. Exercise order checkout, dynamic QR, static QR, refund webhooks, and actual bank settlement using your Razorpay test/staging account. Gateway calls in automated tests are simulated; no real funds were moved.
4. Confirm your Razorpay fee-bearer configuration charges gateway/Route/settlement costs to Bittu's remaining pool. The code controls the merchant transfer amount; it cannot verify your external commercial configuration or guarantee the bank credit from this repository alone.

## Recovery and accounting limits

- Inspect `rzp_route_operations` rows with `state='unknown'`. For transfers/reversals, replaying the failed webhook first reads gateway state. If the completed movement is visible, it can reconcile without sending again. If the result remains ambiguous, investigate the gateway operation; do not delete the row or blindly reset it to resend.
- A payment already refunded before its first Route transfer requires manual reconciliation; it is deliberately blocked from sending the original full merchant amount. Historical order-owned transfers and tiny static-QR receipts can also require operations review.
- The generic statement breakdown uses an explicitly estimated gateway charge unless actual charges are supplied. Actual gateway fee/tax mirrors and settlement reports remain authoritative; contractual fee treatment and additional fees require settlement verification. This pass does not turn estimates into audited Bittu revenue or rewrite historical invoices.
- Financial database integrations are tested against minimal isolated PostgreSQL schemas, not a full production database clone. Production-like integration and real Razorpay settlement testing remain deployment checks.
- Previous, unfinished inventory edits in this working tree were preserved and were not included in this payment validation.

## Validation

Tests cover fixed merchant entitlement under changing gateway charges, paise conservation, concurrent captures, historical splits, ownership/amount checks, uncertain HTTP responses, partial/refunded status, cumulative and repeated reversals, webhook reclaim, client refund deduplication, reporting, settlement pagination, and database tenant settings. Existing auth and state-machine tests also pass.

Resumed review on 2026-09-25: **54 tests passed** using the command below against isolated PostgreSQL and simulated gateway calls. Added coverage checks persisted timeout recovery, mismatched recovery amounts, failed/wrong-recipient/wrong-amount creation responses, and rejection of mixed recipients before any refund reversal.

The full suite collected 193 tests: **188 passed, 5 failed**. All five failures in `tests/test_checkout_idempotency.py` were reproduced unchanged from a clean archive of committed HEAD `28f9c7c`: four tests reach an unmocked database transaction without an initialized pool, and one expects a date string instead of the UTC datetime produced by the date filter. These pre-existing checkout tests were not changed as part of the Razorpay work. Python compilation and `git diff --check` also passed.

Command (Python 3.12; `pgserver` supplies isolated PostgreSQL):

```sh
python -m pytest tests/test_fee_policy.py tests/test_route_payment_safety.py tests/test_state_machines.py tests/test_auth.py -q
```

Protocol reference: [Razorpay Route transfer entity](https://razorpay.com/docs/api/payments/route/transfers-entity/) and the supplied `app/Razorpay.json` collection. Collection examples do not prove production account settings.
