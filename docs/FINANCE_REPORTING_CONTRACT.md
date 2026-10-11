# Merchant finance reporting

The commercial policy remains a flat 5% total deduction on new integrated online payments, rounded once in integer paise. Gateway charges reduce the platform's residual; they never reduce the saved merchant entitlement. Cash, counter and COD have no deduction. SaaS subscription and device prices retain their separate contracts.

## Canonical read model

`razorpay/finance_queries.py` supplies the shared payment and settlement queries. `/merchant-wallet`, `/merchant-ledger`, `/statements`, merchant report wallets, and newly generated `/merchant-statements` use `merchant_finance_service`.

- Captured online payments retain `rzp_route_allocations.merchant_paise`, with original nonfailed transfers as the historical fallback. Missing allocations are reported as unresolved, with null money fields. Today's rate is never applied to historical payments.
- Online bank liability is original entitlement minus proportional processed refunds minus confirmed merchant bank payouts. Cumulative refund allocation recovers rounding on the final refund. Transfers are informational ledger entries, so they do not credit the entitlement twice.
- Available clearing balance includes processed transfers only. KYC state is reported separately and does not erase an existing liability.
- Cash is recorded as receipts less recorded refunds, outside online clearing, pending settlements and escrow. Cash records have mutable status rather than timestamped refund events: historical cash snapshots reflect their currently recorded refund state.
- Payment rows are paginated after cash and online sources are combined. Date filters use Asia/Kolkata. Activity uses the requested date window; balances use all history through its upper cutoff.
- A bank payout requires a processed settlement header attributed to that merchant. A platform or unattributed settlement does not confirm merchant bank payment. Headers without reconciliation lines still show the actual payout amount. Zero net amounts stay zero. Branch payout figures require attributable reconciliation lines.
- Settlement links on transfers can confirm payment settlement before reconciliation lines arrive. A settlement timeline reports the observed state and known timestamp; it sets `history_complete=false` rather than inventing earlier transitions.

These are local mirrors updated by gateway webhooks and polling, not synchronous reads of live gateway balances. The projection describes online entitlement and clearing, rather than physical cash deposits, bank statement reconciliation or a full dispute reserve model.

## Deduction and fee fields

`total_deduction` means gross less original merchant entitlement. `platform_commission` and `commission_amount` remain compatibility aliases for that combined deduction, including gateway charges and platform GST; they are not platform revenue.

Actual `rzp_payments.fee_paise` includes gateway GST, matching the [Razorpay payment entity definition](https://razorpay.com/docs/api/payments/entity/). `tax_paise` is informational and must not be added to fee again. Fee allocation requires both the original merchant allocation and an actual gateway fee. Unknown components remain null and counts identify missing records; `known_subtotals` carries explicitly partial totals.

Fee/GST allocation summaries are based on captured payments before refund adjustments. They are not tax invoices or tax-return totals. Estimates appear in previews only and are labelled. `BITTU_RZP_ESTIMATE_RATE` loads from `.env` or the process environment, with environment values taking precedence; it defaults to `0.0165`. All previews, including the legacy fee calculators, use this same policy.

When gateway costs exceed the total deduction, the response shows a platform loss and zero platform fee/GST, retaining the original merchant entitlement. Quote components are rounded to paise and conserve the payment amount after accounting for the loss.

## Retired settlement writes

`POST /statements/payments/{id}/enqueue` and the manual settlement advance endpoint return 410. Their service entry points also reject writes. The old batch journal, daily-closing and ledger posting paths have been removed from `statement_service`.

Historical `bittu_settlements` and their audit tables are retained in the database. They do not contribute to live Route reports. No historical journal or immutable ledger is automatically rewritten.

## Period statements

New period statements cover online Route entitlement, using the same ledger projection as the wallet. The period is half-open `[period_start, period_end)`; timezone-less inputs are interpreted as UTC. The opening balance includes all earlier events. Missing allocations prevent generating a finalized financial statement.

Generation reads the mirrors in a repeatable-read transaction and saves the summary and entries together. Statement metadata identifies `financial_source=razorpay_route_mirror`, `projection_version=2` and `balance_basis=online_route_entitlement`. Entry snapshots are stored under the internal `_entries` metadata key. Public metadata omits this key; entry and CSV endpoints read the saved snapshot. CSV exports include every saved entry, without the previous 5,000-row truncation. A legitimate zero closing balance is retained.

Existing statements retain their original `legacy_merchant_ledger` basis and report `entries_frozen=false`; new snapshots do not silently alter historical statements.

## Fee plans and bookkeeping roles

Admin fee plans, rules and overrides are explicitly marked `pricing_mode=simulation` and `applies_to_route=false`. `/admin/fee-plans/effective-policy` reports the active Route rule. Draft computation responses include the effective Route quote separately. Simulation active/default flags cannot change payment capture pricing.

Integrated onboarding plans expose `total_deduction_percent=5`, `gateway_charges_included=true` and an explicit basis for the legacy ex-GST `transaction_fee_percent`.

`razorpay/ledgers.py` is the capture bridge for canonical local payments: gross merchant receipt and escrow hold participate in the same transaction, use existing idempotency keys and propagate failures for retry. Static QR receipts without a canonical local payment are accounted for by their gateway mirrors and frozen Route allocation. Cash does not create escrow holds.

The operational merchant ledger, escrow holds and restaurant ERP journals retain their separate audit/operational purposes. They are not summed into a second merchant wallet. Confirming a bank payout cannot be done by manually advancing a legacy Bittu batch.

## Validation and deployment

`tests/test_finance_reporting.py` exercises historical split preservation, actual gateway fee tax handling, cash, unified pagination, tenant and branch scope, pending transfer availability, original proportional refunds, opening balances, zero/failed/foreign bank payouts, settlement back-links, frozen statements, retired writes, strict capture integration and `.env` estimate precedence using isolated PostgreSQL.

This read model requires the existing Route allocation migration `086_route_payment_safety.sql`, as well as the settlement-link migration `070_route_phase8_recon_and_links.sql`. It introduces no additional schema migration. Restart the backend to load the updated code and estimate configuration. Historical data correction remains a separate reconciliation operation.

## Business decisions (2026-10-11)

**D1 — GST basis: `full_consideration`.** Bittu's taxable supply to the merchant is the entire 5% deduction: `bittu_fee = round_half_up(deduction / 1.18)` (4.2373% of gross), `gst_on_fee = deduction − bittu_fee` (0.7627%), rounded once per payment in paise so fee + GST == deduction exactly and invoices reconcile to ledger legs. Razorpay's charges are Bittu's input cost (`razorpay_cut`, `platform_margin`, `platform_loss`) and never reduce the taxable fee. `fee_policy` is the single source: plans (`/onboarding/plans`), `rate_card()`, `quote_fee()`, statements, the merchant ledger legs and the Bittu→merchant SaaS tax invoice all derive from it. The misleading `BITTU_FEE_RATE` constant was renamed `PLATFORM_RESIDUAL_RATE_ESTIMATE`.

**D2 — Direct POS collections.** `fee_policy.classify_method(method, source)` is the only classifier (`cash` | `direct` | `razorpay`). UPI/card/wallet recorded at the POS without a gateway (`source="pos"`) are `direct`: completed on receipt, no deduction, no escrow, reported alongside cash with their real method (`finance_queries` `local` CTE, `source='direct'`). A payment with Razorpay ids is always `razorpay`. Cash/direct partial refunds are read from `refunds` (status `succeeded`), not from the all-or-nothing `payments.status`.

**D3 — Escrow retired for Route.** No escrow hold is created for any capture (Route pays the merchant's 95% directly). `/escrow/*` is deprecated and read-only; `scripts/escrow_zero_out.py` releases legacy holds so the ledger reaches zero.

**Entitlement ledger.** `merchant_ledger` is an entitlement ledger that reaches zero: `payment_received` (+gross), `fee_deduction` and `gst_deduction` (from the frozen allocation, same transaction, via `razorpay/ledgers.apply_payment_capture` — the single posting path), `settlement_completed` / `settlement_reversed` per merchant-attributed settlement line (`apply_settlement_processed`, called from the webhook and the settlement poller), `refund` (gross) + `fee_reversal`/`gst_reversal` credits so the net equals the merchant's proportional share, `chargeback` with the same credits. Lost disputes also claw back the merchant share through Route (`reverse_refunded_share` includes lost disputes in its cumulative base); failure leaves `notes.route_reversal_state='recovery_pending'` and retries on webhook replay. `scripts/backfill_entitlement_ledger.py` posts missing legs for resolved history and skips unresolved allocations.

**SaaS tax invoice.** Computed from the Route mirror for the IST month (online rows only); refunds processed in the month credit fee + GST proportionally; months with unresolved allocations stay `draft` (watermarked); a closed, fully resolved month is frozen as `issued` and never rewritten — later refunds become `bittu_saas_credit_notes` and flip the invoice to `credit_noted`. Migrations: `090` (invoice columns, credit notes, `fee_reversal`/`gst_reversal` enum values), `091` (distinct AR 1007 / Bank 1008 / Card 1003 accounts with reclass journals), `092` (fee-plan simulation default = live 5%).

**Discounts.** One policy in `tax_engine`: an order-level discount reduces the taxable value pro-rata on each line's subtotal basis and GST is re-derived (exclusive: tax on the reduced price; inclusive MRP: reduce then reverse-derive); odd tax paise land on SGST. `OrderService.apply_discount` recomputes the whole bill through the engine and persists subtotal, discount, CGST/SGST, round-off, total and per-line snapshots; the customer invoice renders persisted fields only.
