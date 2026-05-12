# Database Deprecation Matrix

Last updated: 2026-05-12 · After migration 055.

This document classifies every non-trivial table in the schema as **KEEP**, **MIGRATE**, or **DROP** and records the reasoning so future cleanups don't have to re-derive it.

## Why the schema looks "confusing"

Three accumulated generations:

| Gen | Era | Style | Tables (examples) |
|---|---|---|---|
| **v1** | Initial MVP | Flat per-feature tables | `accounting_entries`, `cash_transactions`, `bank_statements`, `bank_reconciliation`, `audit_log` |
| **v2** | Double-entry GL | Journal-based ledger with COA | `journal_entries`, `journal_lines`, `chart_of_accounts`, `bank_recon_items` |
| **v3** | Event-sourced fintech | Outbox + monthly partitions + idempotency | `financial_events`, `merchant_ledger_y2026mNN`, `escrow_ledger_y2026mNN`, `idempotency_keys`, `outbox_events` |

The 24 `*_y2026mNN` / `*_y2027mNN` tables are **Postgres declarative monthly partitions** of two logical tables (`merchant_ledger`, `escrow_ledger`). The `*_default` partitions catch out-of-range rows and are required by Postgres — not duplicates.

---

## KEEP (active, canonical)

### Identity & RBAC
- `users` (merchants), `branches`, `branch_users`, `staff_invites`
- `roles`, `permissions`, `role_permissions`, `user_role_assignments`
- `auth_sessions`, `mfa_devices`, `password_resets`, `email_verifications`

### Restaurants core
- `items`, `categories`, `modifiers`, `modifier_groups`, `item_modifier_groups`
- `recipes`, `recipe_ingredients`, `ingredients`, `ingredient_categories`
- `tables`, `table_sessions`, `table_session_orders`, `table_session_devices`
- `kitchen_stations`, `item_station_mapping` *(both active — kitchen routing)*
- `staff` *(active — `staff_service.py` legacy operational staff distinct from auth users)*

### Orders / Dine-in
- `orders`, `order_items`, `order_modifiers`, `order_state_history`
- `kots`, `kot_items`, `bills`, `bill_payments`, `split_bills`
- `customers`, `customer_addresses`, `customer_loyalty`

### Payments (v3 — canonical)
- `financial_events` *(event-sourced source of truth)*
- `merchant_ledger`, `merchant_ledger_y*`, `merchant_ledger_default`
- `escrow_ledger`, `escrow_ledger_y*`, `escrow_ledger_default`
- `payment_intents`, `payment_attempts`, `refunds`, `refund_attempts`
- `idempotency_keys`, `outbox_events`, `webhook_events`, `webhook_dlq`
- `bittu_settlements`, `pg_settlements` *(distinct: bittu pays merchant, PG pays bittu)*
- `payout_requests`, `payout_batches`

### Accounting (v2 — canonical for GL)
- `journal_entries`, `journal_lines`, `chart_of_accounts`
- `accounting_periods`, `period_locks`
- `tax_invoices`, `ar_invoices`, `ap_bills`, `ap_payments`
- `gst_returns`, `gstr1_lines`, `gstr3b_summary`
- `bank_recon_items`, `bank_recon_sessions` *(v2 reconciliation)*

### KYC (merchant-level — canonical)
- `merchant_kyc_profiles`, `merchant_kyc_documents`, `merchant_kyc_owners`
- `merchant_kyc_bank_accounts`, `merchant_kyc_audit_events`

### ERP / Inventory
- `purchase_orders`, `purchase_order_items`, `purchase_invoices`, `purchase_invoice_items`
- `vendors`, `vendor_payments`, `vendor_balances`
- `stock_movements`, `stock_adjustments`, `inventory_snapshots`
- `recipes`, `recipe_versions`, `production_runs`

### Fees & Pricing
- `fee_plans`, `fee_plan_rules`, `merchant_fee_overrides`, `fee_computations`

### Misc active
- `sync_logs`, `payment_reminders`, `user_funnel_events`
- `notifications`, `notification_preferences`
- `audit_events` *(v2 — structured)*, `financial_audit_log` *(v3 — fintech-specific)*

---

## MIGRATE then DROP (v1 legacy — still has reads/writes)

These have v3 equivalents but code still touches them. **Audit `app/**/*.py` for usage, port to v3, then drop.**

| v1 table | v3 / v2 replacement | Code touchpoints to migrate |
|---|---|---|
| `accounting_entries` | `journal_entries` + `journal_lines` | `accounting_service.py` (legacy paths) |
| `cash_transactions` | `journal_lines` (cash account) | `cash_service.py` |
| `audit_log` | `audit_events` (structured) | various — replace with `audit_events` writes |
| `bank_statements` + `bank_reconciliation` | `bank_recon_items` + `bank_recon_sessions` | `reconciliation_service.py` v1 paths |
| `invoices` | `tax_invoices` (GST-aware) or `ar_invoices` | `billing_service.list_invoices` — currently still reads `invoices` |
| `daily_analytics` | overlaps with `daily_closings`; pick one | analytics endpoints |

**Process for each row above:**
1. `grep_search` for the table name across `app/**/*.py`.
2. Replace SQL with v2/v3 equivalent.
3. Add migration `0NN_drop_<table>.sql`.
4. Compile, deploy, drop.

---

## DROP (already removed in 054 / 055)

| Table | Migration | Reason |
|---|---|---|
| `user_subscriptions` | 054 | Subscription engine retired |
| `subscription_plans` | 054 | "" |
| `subscription_addons` | 054 | "" |
| `subscription_addon_purchases` | 054 | "" |
| `subscription_invoices` | 054 | "" |
| `subscription_events` | 054 | "" |
| `subscription_payment_history` | 054 | "" |
| `kyc_verifications` | 054 | Legacy user-level KYC (merchant_kyc_* is canonical) |
| `billing_history` | 055 | Was subscription billing |
| `trial_eligibility` | 055 | Was subscription trial |
| `item_profitability` | 055 | Never populated; endpoint computes live |
| `daily_pnl` | 055 | Never populated; endpoint removed |

---

## DROP candidates (not yet executed — need confirmation)

Each requires a `grep_search` audit before dropping.

| Table | Why drop | Risk |
|---|---|---|
| `accounting_entries` | Superseded by `journal_lines` | High — confirm zero v1 writers first |
| `cash_transactions` | Superseded by cash account in GL | High — same |
| `audit_log` | Superseded by `audit_events` | Medium — many writers historically |
| `bank_statements`, `bank_reconciliation` | Superseded by `bank_recon_*` | Medium |
| `invoices` | Superseded by `tax_invoices` / `ar_invoices` | High — `billing_service` still reads it |
| `daily_analytics` | Overlaps `daily_closings` | Low — pick canonical one |

---

## Hard rules (do not break)

- **Always keep** `merchant_kyc_*` — used by `admin_merchant_kyc` via `kyc_service`.
- **Always keep** `fee_plans`, `fee_plan_rules`, `merchant_fee_overrides`, `fee_computations`.
- **Always keep** every `*_default` partition — Postgres requires them.
- Before any `DROP TABLE`: `grep_search` `app/**/*.py` for the table name. If anything references it, port the code first.
- Standard deploy: edit → `python -m compileall app main.py` → commit → push → ssh EC2 → `git pull --ff-only` → `systemctl restart bittu.service` → `is-active` check.
