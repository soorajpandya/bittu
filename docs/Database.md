## Table `accounting_entries`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `restaurant_id` | `text` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `entry_type` | `varchar` |  |
| `amount` | `numeric` |  |
| `payment_method` | `varchar` |  Nullable |
| `category` | `varchar` |  Nullable |
| `reference_type` | `varchar` |  Nullable |
| `reference_id` | `text` |  Nullable |
| `description` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `journal_entry_id` | `uuid` |  Nullable |
| `restaurant_id_uuid` | `uuid` |  Nullable |
| `entry_side` | `text` |  Nullable |
| `account_id` | `uuid` |  Nullable |

## Table `accounting_periods`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `period_start` | `date` |  |
| `period_end` | `date` |  |
| `status` | `varchar` |  |
| `closed_by` | `text` |  Nullable |
| `closed_at` | `timestamptz` |  Nullable |
| `reopened_by` | `text` |  Nullable |
| `reopened_at` | `timestamptz` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `accounting_rules`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `event_type` | `varchar` |  |
| `rule_name` | `varchar` |  |
| `description` | `text` |  Nullable |
| `debit_account_code` | `varchar` |  |
| `credit_account_code` | `varchar` |  |
| `amount_field` | `varchar` |  |
| `amount_multiplier` | `numeric` |  |
| `conditions` | `jsonb` |  |
| `priority` | `int4` |  |
| `is_active` | `bool` |  |
| `reference_type_override` | `varchar` |  Nullable |
| `description_template` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `activity_logs`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `action` | `varchar` |  |
| `entity_type` | `varchar` |  |
| `entity_id` | `uuid` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |

## Table `alerts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `type` | `varchar` |  |
| `severity` | `varchar` |  |
| `title` | `varchar` |  |
| `message` | `text` |  Nullable |
| `reference_type` | `varchar` |  Nullable |
| `reference_id` | `text` |  Nullable |
| `is_read` | `bool` |  |
| `is_dismissed` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `ar_invoice_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `invoice_id` | `uuid` |  |
| `item_name` | `varchar` |  |
| `hsn_code` | `varchar` |  Nullable |
| `quantity` | `numeric` |  |
| `unit_price` | `numeric` |  |
| `discount` | `numeric` |  |
| `taxable_value` | `numeric` |  |
| `cgst_rate` | `numeric` |  |
| `sgst_rate` | `numeric` |  |
| `igst_rate` | `numeric` |  |
| `cgst_amount` | `numeric` |  |
| `sgst_amount` | `numeric` |  |
| `igst_amount` | `numeric` |  |
| `total` | `numeric` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `ar_invoices`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `invoice_number` | `varchar` |  |
| `invoice_date` | `date` |  |
| `due_date` | `date` |  Nullable |
| `customer_id` | `uuid` |  Nullable |
| `customer_name` | `varchar` |  Nullable |
| `customer_gstin` | `varchar` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `subtotal` | `numeric` |  |
| `discount_amount` | `numeric` |  |
| `cgst` | `numeric` |  |
| `sgst` | `numeric` |  |
| `igst` | `numeric` |  |
| `cess` | `numeric` |  |
| `total_amount` | `numeric` |  |
| `amount_paid` | `numeric` |  |
| `balance_due` | `numeric` |  |
| `status` | `varchar` |  |
| `invoice_type` | `varchar` |  |
| `journal_entry_id` | `uuid` |  Nullable |
| `notes` | `text` |  Nullable |
| `terms` | `text` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `audit_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `event_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  Nullable |
| `actor_type` | `text` |  |
| `actor_user_id` | `uuid` |  Nullable |
| `actor_label` | `text` |  Nullable |
| `action` | `text` |  |
| `resource_type` | `text` |  Nullable |
| `resource_id` | `text` |  Nullable |
| `payload` | `jsonb` |  |
| `ip_address` | `inet` |  Nullable |
| `user_agent` | `text` |  Nullable |
| `request_id` | `text` |  Nullable |
| `prev_hash` | `text` |  Nullable |
| `row_hash` | `text` |  |
| `created_at` | `timestamptz` |  |

## Table `audit_log`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `restaurant_id` | `uuid` |  Nullable |
| `user_id` | `text` |  |
| `action` | `varchar` |  Nullable |
| `entity_type` | `varchar` |  Nullable |
| `entity_id` | `text` |  Nullable |
| `new_data` | `jsonb` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `bank_recon_accounts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `account_label` | `text` |  |
| `bank_name` | `text` |  Nullable |
| `account_number_last4` | `varchar` |  Nullable |
| `ifsc` | `varchar` |  Nullable |
| `currency` | `bpchar` |  |
| `is_active` | `bool` |  |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `bank_recon_discrepancies`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `run_id` | `uuid` |  Nullable |
| `merchant_id` | `uuid` |  |
| `account_id` | `uuid` |  Nullable |
| `kind` | `bank_recon_discrepancy_kind` |  |
| `severity` | `varchar` |  |
| `line_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `escrow_entry_id` | `uuid` |  Nullable |
| `expected_amount` | `numeric` |  Nullable |
| `actual_amount` | `numeric` |  Nullable |
| `variance_amount` | `numeric` |  Nullable |
| `notes` | `text` |  Nullable |
| `status` | `bank_recon_discrepancy_status` |  |
| `resolution_notes` | `text` |  Nullable |
| `resolved_at` | `timestamptz` |  Nullable |
| `resolved_by` | `uuid` |  Nullable |
| `metadata` | `jsonb` |  |
| `detected_at` | `timestamptz` |  |

## Table `bank_recon_imports`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `account_id` | `uuid` |  |
| `source` | `bank_recon_import_source` |  |
| `original_filename` | `text` |  Nullable |
| `row_count` | `int4` |  |
| `rows_inserted` | `int4` |  |
| `rows_skipped` | `int4` |  |
| `status` | `bank_recon_import_status` |  |
| `error_message` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `started_at` | `timestamptz` |  |
| `completed_at` | `timestamptz` |  Nullable |
| `imported_by` | `uuid` |  Nullable |

## Table `bank_recon_lines`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `import_id` | `uuid` |  |
| `merchant_id` | `uuid` |  |
| `account_id` | `uuid` |  |
| `posted_date` | `date` |  |
| `value_date` | `date` |  Nullable |
| `amount` | `numeric` |  |
| `currency` | `bpchar` |  |
| `narration` | `text` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `counterparty` | `text` |  Nullable |
| `balance_after` | `numeric` |  Nullable |
| `line_hash` | `text` |  |
| `match_status` | `bank_recon_line_status` |  |
| `matched_settlement_id` | `uuid` |  Nullable |
| `matched_escrow_entry_id` | `uuid` |  Nullable |
| `match_confidence` | `numeric` |  Nullable |
| `matched_at` | `timestamptz` |  Nullable |
| `matched_by` | `text` |  Nullable |
| `raw_row` | `jsonb` |  |
| `created_at` | `timestamptz` |  |

## Table `bank_recon_runs`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  Nullable |
| `account_id` | `uuid` |  Nullable |
| `scope_from` | `date` |  Nullable |
| `scope_to` | `date` |  Nullable |
| `triggered_by` | `uuid` |  Nullable |
| `is_admin_run` | `bool` |  |
| `status` | `bank_recon_run_status` |  |
| `summary` | `jsonb` |  |
| `error_message` | `text` |  Nullable |
| `started_at` | `timestamptz` |  |
| `completed_at` | `timestamptz` |  Nullable |

## Table `bank_reconciliation`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `bank_statement_id` | `uuid` |  |
| `journal_entry_id` | `uuid` |  |
| `match_type` | `varchar` |  |
| `match_confidence` | `numeric` |  Nullable |
| `matched_by` | `varchar` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `bank_statements`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `statement_date` | `date` |  |
| `value_date` | `date` |  Nullable |
| `description` | `text` |  Nullable |
| `reference` | `varchar` |  Nullable |
| `amount` | `numeric` |  |
| `running_balance` | `numeric` |  Nullable |
| `bank_account` | `varchar` |  Nullable |
| `transaction_type` | `varchar` |  Nullable |
| `status` | `varchar` |  |
| `matched_at` | `timestamptz` |  Nullable |
| `import_batch_id` | `varchar` |  Nullable |
| `raw_data` | `jsonb` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `billing_history`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `subscription_id` | `int4` |  Nullable |
| `invoice_number` | `varchar` |  Nullable |
| `razorpay_payment_id` | `varchar` |  Nullable |
| `amount` | `numeric` |  Nullable |
| `status` | `varchar` |  Nullable |
| `paid_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `bittu_settlement_timeline`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `settlement_id` | `uuid` |  |
| `restaurant_id` | `uuid` |  |
| `event_type` | `varchar` |  |
| `title` | `varchar` |  |
| `description` | `text` |  Nullable |
| `from_status` | `varchar` |  Nullable |
| `to_status` | `varchar` |  Nullable |
| `actor_id` | `text` |  Nullable |
| `actor_type` | `varchar` |  |
| `metadata` | `jsonb` |  |
| `occurred_at` | `timestamptz` |  |

## Table `bittu_settlement_transactions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `settlement_id` | `uuid` |  |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `gross_amount` | `numeric` |  |
| `fee_amount` | `numeric` |  |
| `gst_amount` | `numeric` |  |
| `net_amount` | `numeric` |  |
| `transaction_type` | `varchar` |  |
| `payment_method` | `varchar` |  Nullable |
| `customer_name` | `varchar` |  Nullable |
| `order_reference` | `text` |  Nullable |
| `settlement_status` | `varchar` |  |
| `created_at` | `timestamptz` |  |

## Table `bittu_settlements`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `settlement_reference` | `varchar` |  Unique |
| `gross_amount` | `numeric` |  |
| `bittu_fee_amount` | `numeric` |  |
| `gst_amount` | `numeric` |  |
| `net_settlement_amount` | `numeric` |  |
| `fee_rate` | `numeric` |  |
| `gst_rate` | `numeric` |  |
| `settlement_status` | `varchar` |  |
| `settlement_cycle` | `varchar` |  |
| `expected_settlement_at` | `timestamptz` |  Nullable |
| `settled_at` | `timestamptz` |  Nullable |
| `bank_reference_number` | `varchar` |  Nullable |
| `retry_count` | `int2` |  |
| `failure_reason` | `text` |  Nullable |
| `last_attempt_at` | `timestamptz` |  Nullable |
| `journal_entry_id` | `uuid` |  Nullable |
| `period_start` | `timestamptz` |  Nullable |
| `period_end` | `timestamptz` |  Nullable |
| `idempotency_key` | `varchar` |  Nullable Unique |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `branch_users`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `text` | Primary |
| `branch_id` | `uuid` | Primary |
| `owner_id` | `text` |  |
| `role` | `varchar` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `role_id` | `uuid` |  Nullable |

## Table `cash_drawers`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  |
| `name` | `varchar` |  |
| `is_active` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `cash_transactions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `type` | `varchar` |  |
| `amount` | `numeric` |  |
| `description` | `text` |  Nullable |
| `category` | `varchar` |  Nullable |
| `payment_method` | `varchar` |  |
| `created_at` | `timestamptz` |  |

## Table `categories`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `name` | `varchar` |  |
| `slug` | `varchar` |  Nullable |
| `description` | `text` |  Nullable |
| `image_url` | `text` |  Nullable |
| `sort_order` | `int4` |  Nullable |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `chart_of_accounts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `account_code` | `varchar` |  |
| `name` | `varchar` |  |
| `account_type` | `varchar` |  |
| `parent_id` | `uuid` |  Nullable |
| `description` | `text` |  Nullable |
| `is_system` | `bool` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `system_code` | `varchar` |  Nullable |

## Table `checkout_idempotency`

Durable idempotency store for POST /orders/checkout.
     Scoped to (idempotency_key, user_id) to prevent cross-tenant replay.
     response_payload stores the full committed response so retries never
     need to re-query the orders table.

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `idempotency_key` | `varchar` |  |
| `user_id` | `text` |  |
| `order_id` | `uuid` |  Nullable |
| `response_payload` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `expires_at` | `timestamptz` |  |

## Table `combo_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `combo_id` | `int4` |  |
| `item_id` | `int4` |  |
| `quantity` | `int4` |  |

## Table `combos`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `name` | `varchar` |  |
| `description` | `text` |  Nullable |
| `price` | `numeric` |  |
| `image_url` | `text` |  Nullable |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `coupon_usage`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `coupon_id` | `int4` |  |
| `customer_id` | `int4` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `user_id` | `text` |  Nullable |
| `used_at` | `timestamptz` |  |

## Table `coupons`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `code` | `varchar` |  |
| `title` | `varchar` |  Nullable |
| `type` | `varchar` |  |
| `discount_value` | `numeric` |  |
| `min_order_value` | `numeric` |  Nullable |
| `max_discount` | `numeric` |  Nullable |
| `usage_limit` | `int4` |  Nullable |
| `user_usage_limit` | `int4` |  Nullable |
| `valid_from` | `timestamptz` |  Nullable |
| `valid_until` | `timestamptz` |  Nullable |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `customer_addresses`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `customer_id` | `int4` |  |
| `label` | `varchar` |  |
| `address_line` | `text` |  |
| `city` | `varchar` |  Nullable |
| `state` | `varchar` |  Nullable |
| `pincode` | `varchar` |  Nullable |
| `lat` | `numeric` |  Nullable |
| `lng` | `numeric` |  Nullable |
| `is_default` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `customer_ledger`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `customer_id` | `uuid` |  |
| `journal_entry_id` | `uuid` |  |
| `debit` | `numeric` |  |
| `credit` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `reference_type` | `varchar` |  |
| `reference_id` | `varchar` |  Nullable |
| `description` | `text` |  Nullable |
| `entry_date` | `date` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `customers`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `name` | `varchar` |  |
| `email` | `varchar` |  Nullable |
| `phone_number` | `varchar` |  Nullable |
| `address` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `daily_analytics`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `branch_id` | `uuid` |  |
| `date` | `date` |  |
| `total_orders` | `int4` |  |
| `completed_orders` | `int4` |  |
| `cancelled_orders` | `int4` |  |
| `total_revenue` | `numeric` |  |
| `total_tax` | `numeric` |  |
| `total_discount` | `numeric` |  |
| `avg_order_value` | `numeric` |  |
| `dine_in_orders` | `int4` |  |
| `takeaway_orders` | `int4` |  |
| `delivery_orders` | `int4` |  |
| `cash_orders` | `int4` |  |
| `online_orders` | `int4` |  |
| `top_items` | `jsonb` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `daily_closings`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `closing_date` | `date` |  |
| `status` | `varchar` |  |
| `expected_cash` | `numeric` |  |
| `expected_card` | `numeric` |  |
| `expected_upi` | `numeric` |  |
| `actual_cash` | `numeric` |  Nullable |
| `actual_card` | `numeric` |  Nullable |
| `actual_upi` | `numeric` |  Nullable |
| `cash_difference` | `numeric` |  Nullable |
| `card_difference` | `numeric` |  Nullable |
| `upi_difference` | `numeric` |  Nullable |
| `total_orders` | `int4` |  |
| `total_revenue` | `numeric` |  |
| `total_refunds` | `numeric` |  |
| `total_discounts` | `numeric` |  |
| `total_expenses` | `numeric` |  |
| `net_revenue` | `numeric` |  |
| `notes` | `text` |  Nullable |
| `counted_by` | `text` |  Nullable |
| `counted_at` | `timestamptz` |  Nullable |
| `reviewed_by` | `text` |  Nullable |
| `reviewed_at` | `timestamptz` |  Nullable |
| `closed_by` | `text` |  Nullable |
| `closed_at` | `timestamptz` |  Nullable |
| `period_locked` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `total_pending_settlement` | `numeric` |  |
| `total_settled_today` | `numeric` |  |
| `total_bittu_fees` | `numeric` |  |
| `total_failed_settlements` | `numeric` |  |

## Table `daily_pnl`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  |
| `pnl_date` | `date` |  |
| `total_revenue` | `numeric` |  Nullable |
| `total_cogs` | `numeric` |  Nullable |
| `gross_profit` | `numeric` |  Nullable |
| `operating_expenses` | `numeric` |  Nullable |
| `net_profit` | `numeric` |  Nullable |
| `tax_collected` | `numeric` |  Nullable |
| `total_orders` | `int4` |  Nullable |
| `avg_order_value` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `deliverable_pincodes`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `pincode` | `varchar` |  |
| `area_name` | `varchar` |  Nullable |
| `city` | `varchar` |  Nullable |
| `state` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `deliveries`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `order_id` | `uuid` |  |
| `restaurant_id` | `uuid` |  |
| `status` | `varchar` |  |
| `pickup_address` | `text` |  Nullable |
| `delivery_address` | `text` |  |
| `delivery_phone` | `varchar` |  Nullable |
| `partner_id` | `uuid` |  Nullable |
| `assigned_at` | `timestamptz` |  Nullable |
| `picked_up_at` | `timestamptz` |  Nullable |
| `delivered_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `delivery_partners`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `name` | `varchar` |  |
| `phone` | `varchar` |  Nullable |
| `status` | `varchar` |  |
| `is_active` | `bool` |  |
| `latitude` | `numeric` |  Nullable |
| `longitude` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `delivery_tracking`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `delivery_id` | `uuid` |  |
| `partner_id` | `uuid` |  |
| `latitude` | `numeric` |  Nullable |
| `longitude` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `dine_in_sessions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `table_id` | `uuid` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `session_token` | `varchar` |  |
| `device_id` | `varchar` |  Nullable |
| `guest_count` | `int4` |  Nullable |
| `status` | `varchar` |  |
| `active_order_id` | `uuid` |  Nullable |
| `last_activity_at` | `timestamptz` |  |
| `expires_at` | `timestamptz` |  Nullable |
| `ended_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `started_at` | `timestamptz` |  |
| `total_amount` | `numeric` |  |
| `paid_amount` | `numeric` |  |
| `remaining_amount` | `numeric` |  |
| `active_users_count` | `int4` |  |
| `created_by` | `varchar` |  Nullable |
| `merged_into_session_id` | `uuid` |  Nullable |

## Table `dispute_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `dispute_id` | `int8` |  |
| `event_type` | `text` |  |
| `from_status` | `dispute_status_enum` |  Nullable |
| `to_status` | `dispute_status_enum` |  Nullable |
| `payload` | `jsonb` |  |
| `actor_user_id` | `uuid` |  Nullable |
| `actor_admin_id` | `uuid` |  Nullable |
| `actor_label` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `disputes`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `dispute_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `payment_id` | `uuid` |  Nullable |
| `refund_id` | `int8` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `kind` | `dispute_kind_enum` |  |
| `status` | `dispute_status_enum` |  |
| `amount` | `numeric` |  |
| `currency` | `bpchar` |  |
| `customer_reference` | `text` |  Nullable |
| `bank_case_id` | `text` |  Nullable |
| `evidence` | `jsonb` |  |
| `notes` | `jsonb` |  |
| `opened_by_user_id` | `uuid` |  Nullable |
| `opened_by_admin_id` | `uuid` |  Nullable |
| `assigned_admin_id` | `uuid` |  Nullable |
| `outcome` | `dispute_outcome_enum` |  Nullable |
| `resolution_notes` | `text` |  Nullable |
| `ledger_entry_id` | `uuid` |  Nullable |
| `due_at` | `timestamptz` |  Nullable |
| `opened_at` | `timestamptz` |  |
| `resolved_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `due_payments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `customer_id` | `int4` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `total_amount` | `numeric` |  |
| `paid_amount` | `numeric` |  |
| `due_amount` | `numeric` |  Nullable |
| `status` | `varchar` |  |
| `due_date` | `date` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `erp_event_log`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  Nullable |
| `event_type` | `varchar` |  |
| `reference_type` | `varchar` |  Nullable |
| `reference_id` | `text` |  Nullable |
| `status` | `varchar` |  |
| `payload` | `jsonb` |  Nullable |
| `error_message` | `text` |  Nullable |
| `processing_time_ms` | `int4` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `escrow_balance_locks`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `currency` | `bpchar` | Primary |
| `held_balance` | `numeric` |  |
| `last_entry_id` | `uuid` |  Nullable |
| `last_posted_at` | `timestamptz` |  Nullable |
| `version` | `int8` |  |

## Table `escrow_balance_snapshots`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `nodal_account_id` | `uuid` |  |
| `snapshot_date` | `date` |  |
| `snapshot_window_start` | `timestamptz` |  |
| `snapshot_window_end` | `timestamptz` |  |
| `opening_balance` | `numeric` |  |
| `credits` | `numeric` |  |
| `debits` | `numeric` |  |
| `computed_closing` | `numeric` |  |
| `actual_bank_balance` | `numeric` |  Nullable |
| `variance` | `numeric` |  Nullable |
| `variance_paisa` | `int8` |  Nullable |
| `status` | `escrow_snapshot_status` |  |
| `breach_reason` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `matched_at` | `timestamptz` |  Nullable |

## Table `escrow_ledger`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_default`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_idempotency`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `idempotency_key` | `text` | Primary |
| `ledger_id` | `uuid` |  |
| `ledger_created_at` | `timestamptz` |  |
| `created_at` | `timestamptz` |  |

## Table `escrow_ledger_references`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `escrow_reference` | `text` | Primary |
| `ledger_id` | `uuid` |  |
| `ledger_created_at` | `timestamptz` |  |
| `created_at` | `timestamptz` |  |

## Table `escrow_ledger_seq`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `yyyymm` | `bpchar` | Primary |
| `last_seq` | `int8` |  |

## Table `escrow_ledger_y2026m05`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m06`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m07`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m08`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m09`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m10`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m11`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2026m12`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2027m01`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2027m02`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2027m03`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2027m04`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_ledger_y2027m05`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `escrow_reference` | `text` |  |
| `transaction_type` | `escrow_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `hold_until` | `timestamptz` |  Nullable |
| `released_entry_id` | `uuid` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |
| `nodal_account_id` | `uuid` |  Nullable |

## Table `escrow_release_links`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `hold_entry_id` | `uuid` | Primary |
| `release_entry_id` | `uuid` |  |
| `released_amount` | `numeric` |  |
| `created_at` | `timestamptz` |  |

## Table `expense_categories`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `name` | `varchar` |  |
| `account_code` | `varchar` |  |
| `description` | `text` |  Nullable |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `expenses`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `category_id` | `uuid` |  Nullable |
| `category_name` | `varchar` |  Nullable |
| `vendor_id` | `uuid` |  Nullable |
| `vendor_name` | `varchar` |  Nullable |
| `amount` | `numeric` |  |
| `tax_amount` | `numeric` |  |
| `total_amount` | `numeric` |  |
| `payment_method` | `varchar` |  |
| `payment_status` | `varchar` |  |
| `paid_amount` | `numeric` |  |
| `expense_date` | `date` |  |
| `description` | `text` |  Nullable |
| `receipt_url` | `text` |  Nullable |
| `invoice_number` | `varchar` |  Nullable |
| `is_recurring` | `bool` |  |
| `recurrence` | `varchar` |  Nullable |
| `journal_entry_id` | `uuid` |  Nullable |
| `approved_by` | `text` |  Nullable |
| `approved_at` | `timestamptz` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `favourite_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `item_id` | `int4` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `feature_flags`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  Nullable |
| `flag_name` | `varchar` |  |
| `is_enabled` | `bool` |  |
| `metadata` | `jsonb` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `fee_computations`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `computation_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `payment_id` | `text` |  Nullable |
| `plan_id` | `int8` |  |
| `rule_id` | `int8` |  Nullable |
| `payment_method` | `text` |  Nullable |
| `order_source` | `text` |  Nullable |
| `currency` | `bpchar` |  |
| `gross_amount` | `numeric` |  |
| `fee_amount` | `numeric` |  |
| `gst_amount` | `numeric` |  |
| `total_deduction` | `numeric` |  |
| `net_amount` | `numeric` |  |
| `breakdown` | `jsonb` |  |
| `computed_at` | `timestamptz` |  |

## Table `fee_plan_rules`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `rule_uuid` | `uuid` |  Unique |
| `plan_id` | `int8` |  |
| `payment_method` | `text` |  Nullable |
| `order_source` | `text` |  Nullable |
| `min_amount` | `numeric` |  |
| `max_amount` | `numeric` |  Nullable |
| `fee_type` | `fee_calc_type` |  |
| `percent_rate` | `numeric` |  |
| `flat_fee` | `numeric` |  |
| `priority` | `int4` |  |
| `is_active` | `bool` |  |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `fee_plans`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `plan_uuid` | `uuid` |  Unique |
| `code` | `text` |  Unique |
| `name` | `text` |  |
| `description` | `text` |  Nullable |
| `currency` | `bpchar` |  |
| `gst_rate` | `numeric` |  |
| `is_active` | `bool` |  |
| `is_default` | `bool` |  |
| `valid_from` | `timestamptz` |  |
| `valid_to` | `timestamptz` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_by_admin_id` | `uuid` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `feedback`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `customer_id` | `int4` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `rating` | `numeric` |  Nullable |
| `food_rating` | `numeric` |  Nullable |
| `service_rating` | `numeric` |  Nullable |
| `ambience_rating` | `numeric` |  Nullable |
| `comment` | `text` |  Nullable |
| `source` | `varchar` |  |
| `staff_response` | `text` |  Nullable |
| `responded` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `financial_alerts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `alert_type` | `varchar` |  |
| `severity` | `varchar` |  |
| `title` | `varchar` |  |
| `details` | `jsonb` |  |
| `is_resolved` | `bool` |  |
| `resolved_by` | `text` |  Nullable |
| `resolved_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `suggested_action` | `text` |  Nullable |
| `auto_resolve_rule` | `varchar` |  Nullable |
| `notified` | `bool` |  |
| `notified_at` | `timestamptz` |  Nullable |
| `resolution_notes` | `text` |  Nullable |

## Table `financial_audit_log`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `user_id` | `text` |  |
| `action` | `varchar` |  |
| `entity_type` | `varchar` |  |
| `entity_id` | `text` |  Nullable |
| `old_value` | `jsonb` |  Nullable |
| `new_value` | `jsonb` |  Nullable |
| `metadata` | `jsonb` |  |
| `ip_address` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `financial_event_stream_index`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `aggregate_type` | `text` | Primary |
| `aggregate_id` | `uuid` | Primary |
| `stream_version` | `int4` | Primary |
| `event_id` | `uuid` |  |
| `row_hash` | `text` |  |
| `created_at` | `timestamptz` |  |

## Table `financial_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `seq` | `int8` |  |
| `aggregate_type` | `text` |  |
| `aggregate_id` | `uuid` |  |
| `stream_version` | `int4` |  |
| `event_type` | `text` |  |
| `event_version` | `int4` |  |
| `payload` | `jsonb` |  |
| `payload_canonical` | `text` |  |
| `prev_hash` | `text` |  Nullable |
| `row_hash` | `text` |  |
| `correlation_id` | `text` |  Nullable |
| `causation_id` | `uuid` |  Nullable |
| `actor_type` | `text` |  Nullable |
| `actor_id` | `uuid` |  Nullable |
| `occurred_at` | `timestamptz` |  |
| `created_at` | `timestamptz` | Primary |

## Table `financial_events_default`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `seq` | `int8` |  |
| `aggregate_type` | `text` |  |
| `aggregate_id` | `uuid` |  |
| `stream_version` | `int4` |  |
| `event_type` | `text` |  |
| `event_version` | `int4` |  |
| `payload` | `jsonb` |  |
| `payload_canonical` | `text` |  |
| `prev_hash` | `text` |  Nullable |
| `row_hash` | `text` |  |
| `correlation_id` | `text` |  Nullable |
| `causation_id` | `uuid` |  Nullable |
| `actor_type` | `text` |  Nullable |
| `actor_id` | `uuid` |  Nullable |
| `occurred_at` | `timestamptz` |  |
| `created_at` | `timestamptz` | Primary |

## Table `food_images`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `name` | `text` | Primary |
| `image_url` | `text` |  Nullable |
| `image_original_url` | `text` |  Nullable |
| `image_512_url` | `text` |  Nullable |
| `image_256_url` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `goods_receipt_notes`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `purchase_order_id` | `int4` |  Nullable |
| `vendor_id` | `uuid` |  Nullable |
| `grn_number` | `varchar` |  |
| `received_date` | `date` |  Nullable |
| `total_amount` | `numeric` |  Nullable |
| `status` | `varchar` |  Nullable |
| `notes` | `text` |  Nullable |
| `received_by` | `text` |  Nullable |
| `verified_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `google_connections`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `account_id` | `varchar` |  Nullable |
| `location_id` | `varchar` |  Nullable |
| `is_active` | `bool` |  |
| `synced_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  |
| `created_at` | `timestamptz` |  |

## Table `grn_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `grn_id` | `uuid` |  |
| `ingredient_id` | `text` |  |
| `ordered_quantity` | `numeric` |  Nullable |
| `received_quantity` | `numeric` |  |
| `rejected_quantity` | `numeric` |  Nullable |
| `unit` | `varchar` |  Nullable |
| `unit_cost` | `numeric` |  Nullable |
| `line_total` | `numeric` |  Nullable |
| `batch_number` | `varchar` |  Nullable |
| `expiry_date` | `date` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `gst_filing_workflows`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `period_start` | `date` |  |
| `period_end` | `date` |  |
| `status` | `varchar` |  |
| `cgst_collected` | `numeric` |  |
| `sgst_collected` | `numeric` |  |
| `igst_collected` | `numeric` |  |
| `cgst_input` | `numeric` |  |
| `sgst_input` | `numeric` |  |
| `igst_input` | `numeric` |  |
| `net_payable` | `numeric` |  |
| `generated_at` | `timestamptz` |  Nullable |
| `reviewed_by` | `text` |  Nullable |
| `reviewed_at` | `timestamptz` |  Nullable |
| `exported_at` | `timestamptz` |  Nullable |
| `filed_at` | `timestamptz` |  Nullable |
| `filed_reference` | `text` |  Nullable |
| `paid_at` | `timestamptz` |  Nullable |
| `paid_amount` | `numeric` |  Nullable |
| `paid_reference` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `gst_invoice_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `invoice_id` | `int4` |  |
| `item_id` | `int4` |  Nullable |
| `item_name` | `varchar` |  |
| `hsn_code` | `varchar` |  Nullable |
| `quantity` | `int4` |  |
| `unit_price` | `numeric` |  |
| `discount` | `numeric` |  Nullable |
| `taxable_value` | `numeric` |  |
| `cgst_rate` | `numeric` |  Nullable |
| `cgst_amount` | `numeric` |  Nullable |
| `sgst_rate` | `numeric` |  Nullable |
| `sgst_amount` | `numeric` |  Nullable |
| `igst_rate` | `numeric` |  Nullable |
| `igst_amount` | `numeric` |  Nullable |
| `total_amount` | `numeric` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `gst_reports`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `report_type` | `varchar` |  |
| `period_start` | `date` |  |
| `period_end` | `date` |  |
| `total_sales` | `numeric` |  Nullable |
| `total_taxable` | `numeric` |  Nullable |
| `cgst_total` | `numeric` |  Nullable |
| `sgst_total` | `numeric` |  Nullable |
| `igst_total` | `numeric` |  Nullable |
| `total_tax` | `numeric` |  Nullable |
| `b2b_count` | `int4` |  Nullable |
| `b2c_count` | `int4` |  Nullable |
| `report_data` | `jsonb` |  Nullable |
| `status` | `varchar` |  Nullable |
| `generated_at` | `timestamptz` |  Nullable |
| `filed_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `help_articles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `title` | `varchar` |  |
| `category` | `varchar` |  Nullable |
| `content` | `text` |  Nullable |
| `order` | `int4` |  Nullable |
| `is_published` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `idempotency_keys`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `key` | `text` | Primary |
| `session_id` | `text` |  Nullable |
| `result` | `jsonb` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `ingredients`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `text` | Primary |
| `user_id` | `text` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `name` | `varchar` |  |
| `unit` | `varchar` |  Nullable |
| `current_stock` | `numeric` |  |
| `stock_quantity` | `numeric` |  |
| `minimum_stock` | `numeric` |  Nullable |
| `cost_per_unit` | `numeric` |  Nullable |
| `supplier` | `varchar` |  Nullable |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `reorder_level` | `numeric` |  Nullable |
| `category` | `varchar` |  Nullable |
| `storage_type` | `varchar` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `sku` | `varchar` |  Nullable |
| `barcode` | `varchar` |  Nullable |
| `storage_location` | `varchar` |  Nullable |
| `reorder_point` | `numeric` |  Nullable |
| `reorder_quantity` | `numeric` |  Nullable |
| `shelf_life_days` | `int4` |  Nullable |
| `is_perishable` | `bool` |  |
| `track_batches` | `bool` |  |
| `preferred_vendor_id` | `uuid` |  Nullable |
| `deleted_at` | `timestamptz` |  Nullable |

## Table `inventory_adjustments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  |
| `adjustment_type` | `varchar` |  |
| `quantity` | `numeric` |  |
| `unit` | `varchar` |  Nullable |
| `unit_cost` | `numeric` |  Nullable |
| `reason` | `varchar` |  Nullable |
| `notes` | `text` |  Nullable |
| `ledger_event_id` | `uuid` |  Nullable |
| `approved_by` | `text` |  Nullable |
| `approved_at` | `timestamptz` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  |

## Table `inventory_alerts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  Nullable |
| `batch_id` | `uuid` |  Nullable |
| `alert_type` | `varchar` |  |
| `severity` | `varchar` |  |
| `title` | `varchar` |  |
| `message` | `text` |  Nullable |
| `payload` | `jsonb` |  Nullable |
| `status` | `varchar` |  |
| `acknowledged_by` | `text` |  Nullable |
| `acknowledged_at` | `timestamptz` |  Nullable |
| `resolved_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `inventory_analytics`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  |
| `period_date` | `date` |  |
| `consumed_qty` | `numeric` |  |
| `purchased_qty` | `numeric` |  |
| `wasted_qty` | `numeric` |  |
| `transferred_in` | `numeric` |  |
| `transferred_out` | `numeric` |  |
| `adjusted_qty` | `numeric` |  |
| `closing_qty` | `numeric` |  |
| `avg_unit_cost` | `numeric` |  |
| `cogs` | `numeric` |  |
| `waste_value` | `numeric` |  |
| `valuation` | `numeric` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `inventory_batches`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  |
| `batch_number` | `varchar` |  |
| `grn_id` | `uuid` |  Nullable |
| `vendor_id` | `uuid` |  Nullable |
| `received_quantity` | `numeric` |  |
| `remaining_quantity` | `numeric` |  |
| `unit` | `varchar` |  Nullable |
| `unit_cost` | `numeric` |  |
| `received_date` | `date` |  |
| `manufacture_date` | `date` |  Nullable |
| `expiry_date` | `date` |  Nullable |
| `status` | `varchar` |  |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `inventory_count_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `count_id` | `uuid` |  |
| `ingredient_id` | `text` |  |
| `expected_qty` | `numeric` |  |
| `counted_qty` | `numeric` |  Nullable |
| `variance` | `numeric` |  Nullable |
| `unit` | `varchar` |  Nullable |
| `unit_cost` | `numeric` |  Nullable |
| `notes` | `text` |  Nullable |
| `counted_by` | `text` |  Nullable |
| `counted_at` | `timestamptz` |  Nullable |

## Table `inventory_counts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `count_number` | `varchar` |  |
| `count_date` | `date` |  |
| `count_type` | `varchar` |  |
| `status` | `varchar` |  |
| `started_by` | `text` |  Nullable |
| `completed_by` | `text` |  Nullable |
| `approved_by` | `text` |  Nullable |
| `started_at` | `timestamptz` |  Nullable |
| `completed_at` | `timestamptz` |  Nullable |
| `approved_at` | `timestamptz` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `inventory_ledger`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  |
| `transaction_type` | `varchar` |  |
| `quantity_in` | `numeric` |  |
| `quantity_out` | `numeric` |  |
| `unit_cost` | `numeric` |  Nullable |
| `reference_type` | `varchar` |  Nullable |
| `reference_id` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `event_id` | `uuid` |  Nullable Unique |
| `correlation_id` | `uuid` |  Nullable |
| `dedup_key` | `text` |  Nullable |
| `batch_id` | `uuid` |  Nullable |
| `source` | `varchar` |  Nullable |
| `metadata` | `jsonb` |  Nullable |
| `reversed_by` | `uuid` |  Nullable |
| `reverses_event` | `uuid` |  Nullable |
| `occurred_at` | `timestamptz` |  Nullable |

## Table `inventory_snapshots`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  |
| `snapshot_at` | `timestamptz` |  |
| `period` | `varchar` |  |
| `opening_qty` | `numeric` |  |
| `in_qty` | `numeric` |  |
| `out_qty` | `numeric` |  |
| `closing_qty` | `numeric` |  |
| `avg_unit_cost` | `numeric` |  |
| `valuation` | `numeric` |  |
| `last_event_id` | `uuid` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `inventory_transactions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `restaurant_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  Nullable |
| `type` | `varchar` |  |
| `quantity` | `numeric` |  |
| `unit` | `varchar` |  Nullable |
| `reference_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `performed_by` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `inventory_wastage`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  |
| `batch_id` | `uuid` |  Nullable |
| `quantity` | `numeric` |  |
| `unit` | `varchar` |  Nullable |
| `unit_cost` | `numeric` |  Nullable |
| `waste_reason` | `varchar` |  |
| `notes` | `text` |  Nullable |
| `photo_url` | `text` |  Nullable |
| `ledger_event_id` | `uuid` |  Nullable |
| `approved_by` | `text` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  |

## Table `invoices`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `order_id` | `uuid` |  Nullable |
| `invoice_number` | `varchar` |  Nullable |
| `amount` | `numeric` |  Nullable |
| `tax` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `gstin` | `varchar` |  Nullable |
| `customer_gstin` | `varchar` |  Nullable |
| `place_of_supply` | `varchar` |  Nullable |
| `invoice_type` | `varchar` |  Nullable |
| `taxable_amount` | `numeric` |  Nullable |
| `cgst_amount` | `numeric` |  Nullable |
| `sgst_amount` | `numeric` |  Nullable |
| `igst_amount` | `numeric` |  Nullable |
| `discount_amount` | `numeric` |  Nullable |
| `round_off` | `numeric` |  Nullable |
| `is_cancelled` | `bool` |  Nullable |
| `cancelled_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `total_amount` | `numeric` |  Nullable |
| `irn` | `varchar` |  Nullable |
| `ack_number` | `varchar` |  Nullable |
| `ack_date` | `timestamptz` |  Nullable |
| `qr_code` | `text` |  Nullable |

## Table `item_addons`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `item_id` | `int4` |  |
| `user_id` | `text` |  |
| `name` | `varchar` |  |
| `price` | `numeric` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `item_extras`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `item_id` | `int4` |  |
| `user_id` | `text` |  |
| `name` | `varchar` |  |
| `price` | `numeric` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `item_ingredients`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `item_id` | `int4` |  |
| `ingredient_id` | `text` |  Nullable |
| `quantity_used` | `numeric` |  |
| `unit` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `item_profitability`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  |
| `item_id` | `int4` |  |
| `period_start` | `date` |  |
| `period_end` | `date` |  |
| `quantity_sold` | `int4` |  Nullable |
| `total_revenue` | `numeric` |  Nullable |
| `total_cogs` | `numeric` |  Nullable |
| `gross_profit` | `numeric` |  Nullable |
| `margin_percent` | `numeric` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `item_station_mapping`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `item_id` | `int4` |  |
| `station_id` | `int4` |  |
| `created_at` | `timestamptz` |  |

## Table `item_tax_mapping`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `item_id` | `int4` |  |
| `tax_rate_id` | `uuid` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `item_variants`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `item_id` | `int4` |  |
| `user_id` | `text` |  |
| `name` | `varchar` |  |
| `price` | `numeric` |  |
| `is_active` | `bool` |  |
| `sku` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `Item_ID` | `int4` | Primary |
| `Item_Name` | `varchar` |  |
| `Description` | `text` |  Nullable |
| `price` | `numeric` |  |
| `Available_Status` | `bool` |  |
| `Category` | `varchar` |  Nullable |
| `Subcategory` | `varchar` |  Nullable |
| `Cuisine` | `varchar` |  Nullable |
| `Spice_Level` | `varchar` |  Nullable |
| `Prep_Time_Min` | `int4` |  Nullable |
| `Image_url` | `text` |  Nullable |
| `is_veg` | `bool` |  Nullable |
| `tags` | `_text` |  Nullable |
| `sort_order` | `int4` |  Nullable |
| `dine_in_available` | `bool` |  |
| `takeaway_available` | `bool` |  |
| `delivery_available` | `bool` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `user_id` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `food_image_name` | `text` |  Nullable |

## Table `journal_entries`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `entry_date` | `date` |  |
| `reference_type` | `varchar` |  |
| `reference_id` | `text` |  Nullable |
| `description` | `text` |  Nullable |
| `is_reversed` | `bool` |  Nullable |
| `reversed_by` | `uuid` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `reversed_entry_id` | `uuid` |  Nullable |
| `source_event` | `varchar` |  Nullable |

## Table `journal_lines`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `journal_entry_id` | `uuid` |  |
| `account_id` | `uuid` |  |
| `debit` | `numeric` |  |
| `credit` | `numeric` |  |
| `description` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `kitchen_order_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `kitchen_order_id` | `uuid` |  |
| `order_item_id` | `int4` |  Nullable |
| `status` | `varchar` |  |
| `item_id` | `int4` |  Nullable |
| `item_name` | `varchar` |  Nullable |
| `quantity` | `int4` |  |
| `station_id` | `int4` |  Nullable |
| `started_at` | `timestamptz` |  Nullable |
| `ready_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `kitchen_orders`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `order_id` | `uuid` |  |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `status` | `varchar` |  |
| `station` | `varchar` |  Nullable |
| `priority` | `int4` |  Nullable |
| `started_at` | `timestamptz` |  Nullable |
| `ready_at` | `timestamptz` |  Nullable |
| `served_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `kitchen_stations`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `text` | Primary |
| `user_id` | `text` |  |
| `name` | `varchar` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `kyc_verifications`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `verification_id` | `varchar` |  Nullable Unique |
| `status` | `varchar` |  Nullable |
| `aadhaar_number` | `varchar` |  Nullable |
| `pan_number` | `varchar` |  Nullable |
| `dl_number` | `varchar` |  Nullable |
| `verified_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `kyc_data` | `jsonb` |  Nullable |

## Table `merchant_daily_rollups`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `merchant_id` | `uuid` |  |
| `rollup_date` | `date` |  |
| `currency` | `bpchar` |  |
| `orders_count` | `int4` |  |
| `orders_completed_count` | `int4` |  |
| `gross_sales` | `numeric` |  |
| `discounts_total` | `numeric` |  |
| `tax_total` | `numeric` |  |
| `cogs_total` | `numeric` |  |
| `payments_count` | `int4` |  |
| `payments_amount` | `numeric` |  |
| `payments_cash_amount` | `numeric` |  |
| `refunds_count` | `int4` |  |
| `refunds_initiated_amount` | `numeric` |  |
| `refunds_succeeded_amount` | `numeric` |  |
| `refunds_failed_count` | `int4` |  |
| `disputes_opened_count` | `int4` |  |
| `disputes_lost_amount` | `numeric` |  |
| `disputes_won_count` | `int4` |  |
| `ledger_debit` | `numeric` |  |
| `ledger_credit` | `numeric` |  |
| `ledger_net` | `numeric` |  |
| `fees_total` | `numeric` |  |
| `gst_total` | `numeric` |  |
| `chargebacks_total` | `numeric` |  |
| `computed_at` | `timestamptz` |  |
| `computed_by` | `uuid` |  Nullable |
| `source_version` | `int4` |  |

## Table `merchant_escrow_config`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `hold_days` | `int4` |  |
| `enabled` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `merchant_fee_overrides`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `override_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `plan_id` | `int8` |  |
| `valid_from` | `timestamptz` |  |
| `valid_to` | `timestamptz` |  Nullable |
| `reason` | `text` |  Nullable |
| `created_by_admin_id` | `uuid` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `merchant_kyc_audit_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `merchant_id` | `uuid` |  |
| `event_type` | `varchar` |  |
| `from_status` | `merchant_kyc_status` |  Nullable |
| `to_status` | `merchant_kyc_status` |  Nullable |
| `actor_user_id` | `uuid` |  Nullable |
| `actor_admin_id` | `uuid` |  Nullable |
| `reason` | `text` |  Nullable |
| `payload` | `jsonb` |  |
| `created_at` | `timestamptz` |  |

## Table `merchant_kyc_bank_accounts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `account_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `account_holder_name` | `text` |  |
| `account_number_last4` | `varchar` |  |
| `account_number_hash` | `text` |  |
| `ifsc` | `varchar` |  |
| `bank_name` | `text` |  Nullable |
| `branch` | `text` |  Nullable |
| `account_type` | `varchar` |  |
| `is_primary` | `bool` |  |
| `is_verified` | `bool` |  |
| `verification_method` | `varchar` |  Nullable |
| `verification_ref` | `text` |  Nullable |
| `verified_by_admin_id` | `uuid` |  Nullable |
| `verified_at` | `timestamptz` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `merchant_kyc_documents`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `document_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `owner_id` | `int8` |  Nullable |
| `doc_type` | `merchant_kyc_doc_type` |  |
| `file_url` | `text` |  |
| `file_hash` | `text` |  Nullable |
| `mime_type` | `varchar` |  Nullable |
| `size_bytes` | `int8` |  Nullable |
| `status` | `merchant_kyc_doc_status` |  |
| `rejection_reason` | `text` |  Nullable |
| `expires_at` | `timestamptz` |  Nullable |
| `uploaded_by_user_id` | `uuid` |  Nullable |
| `verified_by_admin_id` | `uuid` |  Nullable |
| `verified_at` | `timestamptz` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `merchant_kyc_owners`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `owner_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `full_name` | `text` |  |
| `role` | `merchant_kyc_owner_role` |  |
| `dob` | `date` |  Nullable |
| `pan` | `varchar` |  Nullable |
| `aadhaar_last4` | `varchar` |  Nullable |
| `ownership_pct` | `numeric` |  |
| `email` | `text` |  Nullable |
| `phone` | `text` |  Nullable |
| `is_signatory` | `bool` |  |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `merchant_kyc_profiles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `legal_name` | `text` |  Nullable |
| `business_type` | `merchant_kyc_business_type` |  Nullable |
| `pan` | `varchar` |  Nullable |
| `gstin` | `varchar` |  Nullable |
| `cin` | `varchar` |  Nullable |
| `registered_address` | `jsonb` |  |
| `contact_email` | `text` |  Nullable |
| `contact_phone` | `text` |  Nullable |
| `website` | `text` |  Nullable |
| `status` | `merchant_kyc_status` |  |
| `risk_tier` | `varchar` |  |
| `rejection_reason` | `text` |  Nullable |
| `suspension_reason` | `text` |  Nullable |
| `submitted_at` | `timestamptz` |  Nullable |
| `reviewed_at` | `timestamptz` |  Nullable |
| `reviewed_by_admin_id` | `uuid` |  Nullable |
| `approved_at` | `timestamptz` |  Nullable |
| `suspended_at` | `timestamptz` |  Nullable |
| `version` | `int4` |  |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `merchant_ledger`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_balance_locks`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `currency` | `bpchar` | Primary |
| `current_balance` | `numeric` |  |
| `last_entry_id` | `uuid` |  Nullable |
| `last_posted_at` | `timestamptz` |  Nullable |
| `version` | `int8` |  |

## Table `merchant_ledger_default`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_idempotency`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `idempotency_key` | `text` | Primary |
| `ledger_id` | `uuid` |  |
| `ledger_created_at` | `timestamptz` |  |
| `created_at` | `timestamptz` |  |

## Table `merchant_ledger_references`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `ledger_reference` | `text` | Primary |
| `ledger_id` | `uuid` |  |
| `ledger_created_at` | `timestamptz` |  |
| `created_at` | `timestamptz` |  |

## Table `merchant_ledger_seq`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `yyyymm` | `bpchar` | Primary |
| `last_seq` | `int8` |  |

## Table `merchant_ledger_y2026m05`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m06`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m07`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m08`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m09`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m10`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m11`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2026m12`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2027m01`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2027m02`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2027m03`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2027m04`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_ledger_y2027m05`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `ledger_reference` | `text` |  |
| `transaction_type` | `merchant_ledger_txn_type` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_liability_idempotency`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `idempotency_key` | `text` | Primary |
| `entry_id` | `uuid` |  |
| `created_at` | `timestamptz` |  |

## Table `merchant_liability_ledger`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `liability_kind` | `merchant_liability_kind` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `refund_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payout_id` | `uuid` |  Nullable |
| `dispute_id` | `uuid` |  Nullable |
| `due_at` | `timestamptz` |  Nullable |
| `aged_bucket` | `text` |  Nullable |
| `reversed_entry_id` | `uuid` |  Nullable |
| `reversal_reason` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_liability_ledger_default`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `liability_kind` | `merchant_liability_kind` |  |
| `debit_amount` | `numeric` |  |
| `credit_amount` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `currency` | `bpchar` |  |
| `source_type` | `text` |  Nullable |
| `source_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `refund_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `payout_id` | `uuid` |  Nullable |
| `dispute_id` | `uuid` |  Nullable |
| `due_at` | `timestamptz` |  Nullable |
| `aged_bucket` | `text` |  Nullable |
| `reversed_entry_id` | `uuid` |  Nullable |
| `reversal_reason` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` | Primary |
| `created_by` | `uuid` |  Nullable |

## Table `merchant_statements`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `period_start` | `timestamptz` |  |
| `period_end` | `timestamptz` |  |
| `currency` | `bpchar` |  |
| `opening_balance` | `numeric` |  |
| `total_credits` | `numeric` |  |
| `total_debits` | `numeric` |  |
| `closing_balance` | `numeric` |  |
| `txn_count` | `int4` |  |
| `breakdown` | `jsonb` |  |
| `status` | `statement_status` |  |
| `file_format` | `text` |  Nullable |
| `file_path` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `generated_at` | `timestamptz` |  |
| `generated_by` | `uuid` |  Nullable |
| `cancelled_at` | `timestamptz` |  Nullable |
| `cancelled_by` | `uuid` |  Nullable |
| `cancellation_reason` | `text` |  Nullable |

## Table `modifier_groups`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `name` | `varchar` |  |
| `is_required` | `bool` |  |
| `min_selections` | `int4` |  Nullable |
| `max_selections` | `int4` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `modifier_options`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `group_id` | `int4` |  |
| `name` | `varchar` |  |
| `price` | `numeric` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `nodal_accounts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `code` | `text` |  Unique |
| `label` | `text` |  |
| `kind` | `nodal_account_kind` |  |
| `status` | `nodal_account_status` |  |
| `bank_name` | `text` |  Nullable |
| `bank_ifsc` | `text` |  Nullable |
| `account_number_last4` | `bpchar` |  Nullable |
| `account_number_hash` | `text` |  Nullable |
| `currency` | `bpchar` |  |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `offers`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `title` | `varchar` |  |
| `description` | `text` |  Nullable |
| `discount` | `numeric` |  Nullable |
| `code` | `varchar` |  Nullable |
| `type` | `varchar` |  Nullable |
| `icon` | `text` |  Nullable |
| `expiry_days` | `int4` |  Nullable |
| `is_active` | `bool` |  |
| `valid_from` | `timestamptz` |  Nullable |
| `valid_until` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `order_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `order_id` | `uuid` |  |
| `item_id` | `int4` |  Nullable |
| `variant_id` | `int4` |  Nullable |
| `item_name` | `varchar` |  Nullable |
| `quantity` | `int4` |  |
| `unit_price` | `numeric` |  |
| `total_price` | `numeric` |  |
| `addons` | `jsonb` |  Nullable |
| `extras` | `jsonb` |  Nullable |
| `notes` | `text` |  Nullable |
| `user_id` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `tax_rate_id` | `uuid` |  Nullable |
| `taxable_amount` | `numeric` |  Nullable |
| `cgst_amount` | `numeric` |  Nullable |
| `sgst_amount` | `numeric` |  Nullable |
| `igst_amount` | `numeric` |  Nullable |
| `tax_total` | `numeric` |  Nullable |
| `hsn_code` | `varchar` |  Nullable |
| `discount_amount` | `numeric` |  Nullable |

## Table `order_tax_details`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `order_id` | `uuid` |  |
| `tax_rate_id` | `uuid` |  Nullable |
| `tax_name` | `varchar` |  Nullable |
| `rate_percentage` | `numeric` |  |
| `taxable_amount` | `numeric` |  |
| `cgst_amount` | `numeric` |  |
| `sgst_amount` | `numeric` |  |
| `igst_amount` | `numeric` |  |
| `total_tax` | `numeric` |  |
| `is_inclusive` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `orders`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `customer_id` | `int4` |  Nullable |
| `source` | `varchar` |  |
| `status` | `varchar` |  |
| `subtotal` | `numeric` |  |
| `tax_amount` | `numeric` |  |
| `discount_amount` | `numeric` |  |
| `total_amount` | `numeric` |  |
| `table_number` | `varchar` |  Nullable |
| `delivery_address` | `text` |  Nullable |
| `delivery_phone` | `varchar` |  Nullable |
| `coupon_id` | `int4` |  Nullable |
| `notes` | `text` |  Nullable |
| `items` | `jsonb` |  Nullable |
| `metadata` | `jsonb` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `cost_of_goods_sold` | `numeric` |  Nullable |
| `order_type` | `varchar` |  Nullable |
| `platform` | `varchar` |  Nullable |
| `is_interstate` | `bool` |  Nullable |
| `gst_handled_externally` | `bool` |  Nullable |
| `invoice_id` | `int4` |  Nullable |
| `shift_id` | `uuid` |  Nullable |

## Table `payment_reminders`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `due_payment_id` | `int4` |  Nullable |
| `reminder_text` | `text` |  Nullable |
| `reminder_date` | `date` |  Nullable |
| `is_sent` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `payment_webhook_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `gateway` | `text` |  |
| `event_id` | `text` |  Nullable |
| `event_type` | `text` |  Nullable |
| `event_hash` | `text` |  |
| `signature_valid` | `bool` |  |
| `processing_state` | `text` |  |
| `retries` | `int4` |  |
| `latency_ms` | `numeric` |  Nullable |
| `last_error` | `text` |  Nullable |
| `headers` | `jsonb` |  |
| `raw_payload` | `jsonb` |  |
| `received_at` | `timestamptz` | Primary |
| `processed_at` | `timestamptz` |  Nullable |

## Table `payment_webhook_events_default`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `gateway` | `text` |  |
| `event_id` | `text` |  Nullable |
| `event_type` | `text` |  Nullable |
| `event_hash` | `text` |  |
| `signature_valid` | `bool` |  |
| `processing_state` | `text` |  |
| `retries` | `int4` |  |
| `latency_ms` | `numeric` |  Nullable |
| `last_error` | `text` |  Nullable |
| `headers` | `jsonb` |  |
| `raw_payload` | `jsonb` |  |
| `received_at` | `timestamptz` | Primary |
| `processed_at` | `timestamptz` |  Nullable |

## Table `payments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `order_id` | `uuid` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `method` | `varchar` |  |
| `status` | `varchar` |  |
| `amount` | `numeric` |  |
| `currency` | `varchar` |  |
| `razorpay_order_id` | `varchar` |  Nullable |
| `razorpay_payment_id` | `varchar` |  Nullable |
| `razorpay_signature` | `text` |  Nullable |
| `paid_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `gateway` | `varchar` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `invoice_id` | `uuid` |  Nullable |

## Table `payout_batch_seq`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `yyyymm` | `bpchar` | Primary |
| `last_seq` | `int8` |  |

## Table `payout_batches`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `batch_reference` | `text` |  Unique |
| `status` | `payout_batch_status` |  |
| `total_amount` | `numeric` |  |
| `total_count` | `int4` |  |
| `currency` | `bpchar` |  |
| `file_format` | `text` |  Nullable |
| `file_generated_at` | `timestamptz` |  Nullable |
| `file_path` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `created_by` | `uuid` |  Nullable |
| `closed_at` | `timestamptz` |  Nullable |

## Table `payout_beneficiaries`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `merchant_id` | `uuid` |  |
| `label` | `text` |  |
| `type` | `payout_beneficiary_type` |  |
| `account_holder` | `text` |  Nullable |
| `account_number` | `text` |  Nullable |
| `account_number_last4` | `text` |  Nullable |
| `ifsc` | `text` |  Nullable |
| `bank_name` | `text` |  Nullable |
| `upi_vpa` | `text` |  Nullable |
| `is_active` | `bool` |  |
| `is_verified` | `bool` |  |
| `verified_at` | `timestamptz` |  Nullable |
| `verified_by` | `uuid` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `created_by` | `uuid` |  Nullable |
| `updated_at` | `timestamptz` |  |

## Table `payout_reference_seq`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `yyyymm` | `bpchar` | Primary |
| `last_seq` | `int8` |  |

## Table `payout_requests`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `payout_reference` | `text` |  Unique |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `beneficiary_id` | `uuid` |  |
| `amount` | `numeric` |  |
| `currency` | `bpchar` |  |
| `method` | `payout_method` |  |
| `status` | `payout_status` |  |
| `requested_by` | `uuid` |  |
| `requested_at` | `timestamptz` |  |
| `approved_by` | `uuid` |  Nullable |
| `approved_at` | `timestamptz` |  Nullable |
| `rejected_by` | `uuid` |  Nullable |
| `rejected_at` | `timestamptz` |  Nullable |
| `rejection_reason` | `text` |  Nullable |
| `cancelled_at` | `timestamptz` |  Nullable |
| `batch_id` | `uuid` |  Nullable |
| `ledger_entry_id` | `uuid` |  Nullable |
| `reversal_entry_id` | `uuid` |  Nullable |
| `utr_number` | `text` |  Nullable |
| `bank_reference` | `text` |  Nullable |
| `sent_at` | `timestamptz` |  Nullable |
| `completed_at` | `timestamptz` |  Nullable |
| `failed_at` | `timestamptz` |  Nullable |
| `failure_reason` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `idempotency_key` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `payout_status_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `payout_id` | `uuid` |  |
| `event_type` | `payout_event_type` |  |
| `from_status` | `payout_status` |  Nullable |
| `to_status` | `payout_status` |  Nullable |
| `actor_user_id` | `uuid` |  Nullable |
| `is_admin_action` | `bool` |  |
| `notes` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |

## Table `permissions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `key` | `varchar` |  Unique |
| `created_at` | `timestamptz` |  |

## Table `pg_settlements`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `gateway` | `varchar` |  |
| `settlement_id` | `varchar` |  Nullable |
| `settlement_date` | `date` |  |
| `gross_amount` | `numeric` |  |
| `gateway_fee` | `numeric` |  |
| `tax_on_fee` | `numeric` |  |
| `net_amount` | `numeric` |  |
| `status` | `varchar` |  |
| `payment_ids` | `_uuid` |  Nullable |
| `clearing_journal_id` | `uuid` |  Nullable |
| `settlement_journal_id` | `uuid` |  Nullable |
| `fee_journal_id` | `uuid` |  Nullable |
| `reconciled_by` | `text` |  Nullable |
| `reconciled_at` | `timestamptz` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `platform_admin_users`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `user_id` | `uuid` | Primary |
| `email` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `created_by` | `uuid` |  Nullable |

## Table `platform_tax_config`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `platform` | `varchar` |  |
| `gst_handled_by_platform` | `bool` |  |
| `commission_rate` | `numeric` |  Nullable |
| `tcs_rate` | `numeric` |  Nullable |
| `notes` | `text` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `purchase_invoice_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `invoice_id` | `uuid` |  |
| `ingredient_id` | `text` |  Nullable |
| `item_name` | `text` |  |
| `hsn_code` | `text` |  Nullable |
| `quantity` | `numeric` |  |
| `unit` | `text` |  Nullable |
| `unit_price` | `numeric` |  Nullable |
| `discount_percent` | `numeric` |  Nullable |
| `tax_percent` | `numeric` |  Nullable |
| `tax_amount` | `numeric` |  Nullable |
| `line_total` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `purchase_invoices`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `restaurant_id` | `text` |  Nullable |
| `branch_id` | `text` |  Nullable |
| `vendor_name` | `text` |  Nullable |
| `vendor_gstin` | `text` |  Nullable |
| `invoice_number` | `text` |  Nullable |
| `invoice_date` | `date` |  Nullable |
| `subtotal` | `numeric` |  Nullable |
| `tax_amount` | `numeric` |  Nullable |
| `total_amount` | `numeric` |  Nullable |
| `payment_status` | `text` |  Nullable |
| `status` | `text` |  Nullable |
| `purchase_order_id` | `text` |  Nullable |
| `raw_ocr_text` | `text` |  Nullable |
| `raw_ai_response` | `jsonb` |  Nullable |
| `idempotency_key` | `text` |  Nullable Unique |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `purchase_order_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `purchase_order_id` | `int4` |  |
| `ingredient_id` | `text` |  Nullable |
| `ingredient_name` | `varchar` |  Nullable |
| `quantity_ordered` | `numeric` |  Nullable |
| `quantity` | `numeric` |  Nullable |
| `unit` | `varchar` |  Nullable |
| `unit_cost` | `numeric` |  Nullable |
| `unit_price` | `numeric` |  Nullable |
| `created_at` | `timestamptz` |  |
| `amount` | `numeric` |  |

## Table `purchase_orders`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `supplier_name` | `varchar` |  Nullable |
| `supplier_contact` | `varchar` |  Nullable |
| `status` | `varchar` |  |
| `notes` | `text` |  Nullable |
| `expected_delivery_date` | `date` |  Nullable |
| `total_amount` | `numeric` |  Nullable |
| `received_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |
| `po_number` | `varchar` |  Nullable |
| `source_type` | `varchar` |  |
| `source_id` | `text` |  Nullable |
| `source_name` | `varchar` |  Nullable |
| `delivery_time` | `time` |  Nullable |
| `sub_total` | `numeric` |  |
| `delivery_charges` | `numeric` |  |
| `payment_status` | `varchar` |  |
| `vendor_id` | `uuid` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |

## Table `recipe_ingredients`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `recipe_id` | `uuid` |  |
| `ingredient_id` | `text` |  |
| `quantity_required` | `numeric` |  |
| `unit` | `varchar` |  Nullable |
| `waste_percent` | `numeric` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `recipes`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `item_id` | `int4` |  |
| `name` | `varchar` |  Nullable |
| `yield_quantity` | `numeric` |  |
| `yield_unit` | `varchar` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `reconciliation_discrepancies`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `run_id` | `uuid` |  |
| `user_id` | `text` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `kind` | `varchar` |  |
| `severity` | `varchar` |  |
| `order_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `settlement_id` | `uuid` |  Nullable |
| `customer_id` | `int4` |  Nullable |
| `expected_amount` | `numeric` |  Nullable |
| `actual_amount` | `numeric` |  Nullable |
| `delta_amount` | `numeric` |  Nullable |
| `description` | `text` |  |
| `metadata` | `jsonb` |  |
| `status` | `varchar` |  |
| `resolved_by` | `text` |  Nullable |
| `resolved_at` | `timestamptz` |  Nullable |
| `resolution_notes` | `text` |  Nullable |
| `detected_at` | `timestamptz` |  |

## Table `reconciliation_runs`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `period_start` | `timestamptz` |  |
| `period_end` | `timestamptz` |  |
| `orders_scanned` | `int4` |  |
| `payments_scanned` | `int4` |  |
| `settlements_scanned` | `int4` |  |
| `discrepancies_found` | `int4` |  |
| `total_order_amount` | `numeric` |  |
| `total_payment_amount` | `numeric` |  |
| `total_settled_amount` | `numeric` |  |
| `total_unsettled_amount` | `numeric` |  |
| `status` | `varchar` |  |
| `triggered_by` | `text` |  |
| `started_at` | `timestamptz` |  |
| `completed_at` | `timestamptz` |  Nullable |
| `error_message` | `text` |  Nullable |

## Table `refunds`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int8` | Primary |
| `refund_uuid` | `uuid` |  Unique |
| `merchant_id` | `uuid` |  |
| `payment_id` | `uuid` |  |
| `order_id` | `uuid` |  Nullable |
| `amount` | `numeric` |  |
| `currency` | `bpchar` |  |
| `kind` | `refund_kind_enum` |  |
| `status` | `refund_status_enum` |  |
| `reason` | `text` |  Nullable |
| `customer_contact` | `text` |  Nullable |
| `gateway_refund_id` | `text` |  Nullable |
| `initiated_by_user_id` | `uuid` |  Nullable |
| `initiated_by_admin_id` | `uuid` |  Nullable |
| `ledger_entry_id` | `uuid` |  Nullable |
| `notes` | `jsonb` |  |
| `failure_reason` | `text` |  Nullable |
| `processed_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `restaurant_settings`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  Unique |
| `restaurant_id` | `uuid` |  Nullable |
| `tax_percentage` | `numeric` |  |
| `currency` | `varchar` |  |
| `receipt_header` | `text` |  Nullable |
| `receipt_footer` | `text` |  Nullable |
| `auto_accept_orders` | `bool` |  |
| `enable_qr_ordering` | `bool` |  |
| `enable_delivery` | `bool` |  |
| `enable_dine_in` | `bool` |  |
| `enable_takeaway` | `bool` |  |
| `printer_config` | `jsonb` |  Nullable |
| `theme_config` | `jsonb` |  Nullable |
| `enable_led_display` | `bool` |  |
| `led_display_url` | `text` |  Nullable |
| `enable_dual_screen` | `bool` |  |
| `dual_screen_url` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `restaurant_tables`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `user_id` | `text` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `table_number` | `varchar` |  |
| `capacity` | `int4` |  Nullable |
| `status` | `varchar` |  |
| `is_active` | `bool` |  |
| `is_occupied` | `bool` |  |
| `occupied_since` | `timestamptz` |  Nullable |
| `session_token` | `varchar` |  Nullable |
| `current_order_id` | `uuid` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `restaurants`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `owner_id` | `text` |  |
| `name` | `varchar` |  |
| `phone` | `varchar` |  Nullable |
| `email` | `varchar` |  Nullable |
| `address` | `text` |  Nullable |
| `city` | `varchar` |  Nullable |
| `state` | `varchar` |  Nullable |
| `pincode` | `varchar` |  Nullable |
| `latitude` | `numeric` |  Nullable |
| `longitude` | `numeric` |  Nullable |
| `logo_url` | `text` |  Nullable |
| `cover_url` | `text` |  Nullable |
| `gst_number` | `varchar` |  Nullable |
| `fssai_number` | `varchar` |  Nullable |
| `is_active` | `bool` |  |
| `opening_time` | `time` |  Nullable |
| `closing_time` | `time` |  Nullable |
| `avg_prep_time` | `int4` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `role_permissions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `role_id` | `uuid` |  |
| `permission_id` | `uuid` |  |
| `allowed` | `bool` |  |
| `meta` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `roles`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `name` | `varchar` |  |
| `branch_id` | `uuid` |  |
| `is_default` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `session_orders`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `session_id` | `uuid` |  |
| `order_id` | `uuid` |  |
| `role` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `shift_transactions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `shift_id` | `uuid` |  |
| `transaction_type` | `varchar` |  |
| `amount` | `numeric` |  |
| `payment_method` | `varchar` |  Nullable |
| `reference_type` | `varchar` |  Nullable |
| `reference_id` | `text` |  Nullable |
| `description` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `shifts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  |
| `drawer_id` | `uuid` |  Nullable |
| `user_id` | `text` |  |
| `opened_at` | `timestamptz` |  |
| `closed_at` | `timestamptz` |  Nullable |
| `opening_cash` | `numeric` |  |
| `closing_cash` | `numeric` |  Nullable |
| `expected_cash` | `numeric` |  Nullable |
| `cash_difference` | `numeric` |  Nullable |
| `status` | `varchar` |  |
| `notes` | `text` |  Nullable |
| `closed_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `staff`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `restaurant_id` | `uuid` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `name` | `varchar` |  |
| `phone` | `varchar` |  Nullable |
| `role` | `varchar` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `staff_invites`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `branch_id` | `uuid` |  |
| `owner_id` | `text` |  |
| `email` | `varchar` |  |
| `role` | `varchar` |  |
| `role_id` | `uuid` |  Nullable |
| `status` | `varchar` |  |
| `accepted_at` | `timestamptz` |  Nullable |
| `expires_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `stock_transfer_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `transfer_id` | `uuid` |  |
| `ingredient_id` | `text` |  |
| `quantity_sent` | `numeric` |  |
| `quantity_received` | `numeric` |  Nullable |
| `unit` | `varchar` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |

## Table `stock_transfers`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `from_branch_id` | `uuid` |  |
| `to_branch_id` | `uuid` |  |
| `transfer_number` | `varchar` |  |
| `status` | `varchar` |  |
| `requested_by` | `text` |  |
| `approved_by` | `text` |  Nullable |
| `received_by` | `text` |  Nullable |
| `shipped_at` | `timestamptz` |  Nullable |
| `received_at` | `timestamptz` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `sub_branches`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `owner_id` | `text` |  |
| `name` | `varchar` |  |
| `is_main_branch` | `bool` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  Nullable |

## Table `subscription_plans`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `name` | `varchar` |  |
| `slug` | `varchar` |  Unique |
| `description` | `text` |  Nullable |
| `price` | `numeric` |  |
| `monthly_price` | `numeric` |  Nullable |
| `currency` | `varchar` |  |
| `interval` | `varchar` |  |
| `features` | `jsonb` |  Nullable |
| `limits` | `jsonb` |  Nullable |
| `not_included` | `jsonb` |  Nullable |
| `highlight` | `bool` |  |
| `highlight_label` | `varchar` |  Nullable |
| `cta_text` | `varchar` |  Nullable |
| `discount_label` | `varchar` |  Nullable |
| `razorpay_plan_id` | `varchar` |  Nullable |
| `is_active` | `bool` |  |
| `sort_order` | `int4` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `supplier_ledger`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `supplier_id` | `uuid` |  |
| `journal_entry_id` | `uuid` |  |
| `debit` | `numeric` |  |
| `credit` | `numeric` |  |
| `balance_after` | `numeric` |  |
| `reference_type` | `varchar` |  |
| `reference_id` | `varchar` |  Nullable |
| `description` | `text` |  Nullable |
| `entry_date` | `date` |  |
| `created_at` | `timestamptz` |  Nullable |

## Table `sync_logs`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `sync_action` | `varchar` |  Nullable |
| `synced_at` | `timestamptz` |  |

## Table `table_session_carts`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `session_id` | `uuid` |  |
| `item_id` | `int4` |  Nullable |
| `variant_id` | `int4` |  Nullable |
| `item_name` | `varchar` |  Nullable |
| `variant_name` | `varchar` |  Nullable |
| `quantity` | `int4` |  |
| `unit_price` | `numeric` |  |
| `total_price` | `numeric` |  |
| `addons` | `jsonb` |  Nullable |
| `extras` | `jsonb` |  Nullable |
| `notes` | `text` |  Nullable |
| `added_by` | `varchar` |  Nullable |
| `request_id` | `varchar` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `table_session_devices`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `session_id` | `uuid` |  |
| `user_id` | `text` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `device_id` | `varchar` |  |
| `device_name` | `varchar` |  Nullable |
| `last_seen` | `timestamptz` |  |
| `is_active` | `bool` |  |
| `joined_at` | `timestamptz` |  |

## Table `table_session_payments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `session_id` | `uuid` |  |
| `order_id` | `uuid` |  Nullable |
| `amount` | `numeric` |  |
| `payment_method` | `varchar` |  |
| `transaction_ref` | `varchar` |  Nullable |
| `paid_by` | `varchar` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `table_session_users`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `session_id` | `uuid` |  |
| `user_type` | `varchar` |  |
| `name` | `varchar` |  Nullable |
| `device_id` | `varchar` |  Nullable |
| `joined_at` | `timestamptz` |  |
| `is_active` | `bool` |  |

## Table `table_sessions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `table_id` | `uuid` |  |
| `restaurant_id` | `uuid` |  Nullable |
| `user_id` | `text` |  |
| `branch_id` | `uuid` |  Nullable |
| `session_token` | `varchar` |  |
| `guest_count` | `int4` |  |
| `customer_count` | `int4` |  |
| `started_at` | `timestamptz` |  |
| `is_active` | `bool` |  |
| `status` | `varchar` |  |
| `expires_at` | `timestamptz` |  Nullable |
| `ended_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `tax_invoice_line_items`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `invoice_id` | `uuid` |  |
| `sno` | `int4` |  |
| `description` | `text` |  |
| `hsn_sac` | `text` |  Nullable |
| `quantity` | `numeric` |  |
| `unit_amount` | `numeric` |  |
| `discount_amount` | `numeric` |  |
| `taxable_amount` | `numeric` |  |
| `cgst_rate` | `numeric` |  |
| `cgst_amount` | `numeric` |  |
| `sgst_rate` | `numeric` |  |
| `sgst_amount` | `numeric` |  |
| `igst_rate` | `numeric` |  |
| `igst_amount` | `numeric` |  |
| `cess_rate` | `numeric` |  |
| `cess_amount` | `numeric` |  |
| `line_total` | `numeric` |  |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |

## Table `tax_invoice_seq`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `merchant_id` | `uuid` | Primary |
| `fy_code` | `bpchar` | Primary |
| `last_seq` | `int8` |  |

## Table `tax_invoices`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `invoice_number` | `text` |  Unique |
| `merchant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `invoice_date` | `date` |  |
| `period_start` | `date` |  Nullable |
| `period_end` | `date` |  Nullable |
| `due_date` | `date` |  Nullable |
| `currency` | `bpchar` |  |
| `subtotal` | `numeric` |  |
| `cgst_total` | `numeric` |  |
| `sgst_total` | `numeric` |  |
| `igst_total` | `numeric` |  |
| `cess_total` | `numeric` |  |
| `discount_total` | `numeric` |  |
| `total_amount` | `numeric` |  |
| `place_of_supply` | `text` |  Nullable |
| `gstin_supplier` | `text` |  Nullable |
| `gstin_customer` | `text` |  Nullable |
| `supplier_name` | `text` |  Nullable |
| `supplier_address` | `text` |  Nullable |
| `customer_name` | `text` |  Nullable |
| `customer_address` | `text` |  Nullable |
| `notes` | `text` |  Nullable |
| `status` | `invoice_status` |  |
| `file_path` | `text` |  Nullable |
| `metadata` | `jsonb` |  |
| `created_at` | `timestamptz` |  |
| `created_by` | `uuid` |  Nullable |
| `issued_at` | `timestamptz` |  Nullable |
| `issued_by` | `uuid` |  Nullable |
| `cancelled_at` | `timestamptz` |  Nullable |
| `cancelled_by` | `uuid` |  Nullable |
| `cancellation_reason` | `text` |  Nullable |
| `updated_at` | `timestamptz` |  |

## Table `tax_liability`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `period_start` | `date` |  |
| `period_end` | `date` |  |
| `period_label` | `varchar` |  Nullable |
| `cgst_collected` | `numeric` |  |
| `sgst_collected` | `numeric` |  |
| `igst_collected` | `numeric` |  |
| `cess_collected` | `numeric` |  |
| `cgst_input` | `numeric` |  |
| `sgst_input` | `numeric` |  |
| `igst_input` | `numeric` |  |
| `cess_input` | `numeric` |  |
| `cgst_payable` | `numeric` |  |
| `sgst_payable` | `numeric` |  |
| `igst_payable` | `numeric` |  |
| `cess_payable` | `numeric` |  |
| `total_payable` | `numeric` |  |
| `status` | `varchar` |  |
| `filed_at` | `timestamptz` |  Nullable |
| `paid_at` | `timestamptz` |  Nullable |
| `payment_reference` | `varchar` |  Nullable |
| `payment_journal_id` | `uuid` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `tax_rates`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `name` | `varchar` |  |
| `hsn_code` | `varchar` |  Nullable |
| `rate_percentage` | `numeric` |  |
| `cgst_percentage` | `numeric` |  |
| `sgst_percentage` | `numeric` |  |
| `igst_percentage` | `numeric` |  |
| `is_inclusive` | `bool` |  Nullable |
| `applicable_on` | `varchar` |  Nullable |
| `is_exempt` | `bool` |  Nullable |
| `is_composition` | `bool` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `tax_rules`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `name` | `varchar` |  |
| `priority` | `int4` |  |
| `tax_rate_id` | `uuid` |  |
| `order_type` | `varchar` |  Nullable |
| `platform` | `varchar` |  Nullable |
| `is_interstate` | `bool` |  Nullable |
| `applicable_on` | `varchar` |  Nullable |
| `min_order_value` | `numeric` |  Nullable |
| `max_order_value` | `numeric` |  Nullable |
| `time_from` | `time` |  Nullable |
| `time_to` | `time` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `trial_eligibility`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  Unique |
| `trial_started_at` | `timestamptz` |  Nullable |
| `trial_expires_at` | `timestamptz` |  Nullable |
| `eligible` | `bool` |  |
| `used` | `bool` |  |
| `used_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `unit_conversions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  Nullable |
| `ingredient_id` | `text` |  Nullable |
| `from_unit` | `varchar` |  |
| `to_unit` | `varchar` |  |
| `factor` | `numeric` |  |
| `is_active` | `bool` |  |
| `created_at` | `timestamptz` |  |

## Table `user_funnel_events`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  Unique |
| `step` | `varchar` |  Nullable |
| `first_seen` | `timestamptz` |  |
| `last_seen` | `timestamptz` |  |
| `visit_count` | `int4` |  |
| `created_at` | `timestamptz` |  |

## Table `user_subscriptions`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `int4` | Primary |
| `user_id` | `text` |  |
| `plan_id` | `int4` |  Nullable |
| `status` | `varchar` |  |
| `trial_started_at` | `timestamptz` |  Nullable |
| `trial_expires_at` | `timestamptz` |  Nullable |
| `trial_end` | `timestamptz` |  Nullable |
| `trial_used` | `bool` |  |
| `razorpay_subscription_id` | `varchar` |  Nullable |
| `current_period_start` | `timestamptz` |  Nullable |
| `current_period_end` | `timestamptz` |  Nullable |
| `grace_period_end` | `timestamptz` |  Nullable |
| `last_payment_at` | `timestamptz` |  Nullable |
| `payment_retry_count` | `int4` |  |
| `cancelled_at` | `timestamptz` |  Nullable |
| `ended_at` | `timestamptz` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `vendor_payments`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `vendor_id` | `uuid` |  |
| `amount` | `numeric` |  |
| `payment_method` | `varchar` |  |
| `payment_date` | `date` |  Nullable |
| `reference_number` | `varchar` |  Nullable |
| `purchase_order_id` | `int4` |  Nullable |
| `grn_id` | `uuid` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_by` | `text` |  |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |
| `journal_entry_id` | `uuid` |  Nullable |

## Table `vendors`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `name` | `varchar` |  |
| `contact_person` | `varchar` |  Nullable |
| `phone` | `varchar` |  Nullable |
| `email` | `varchar` |  Nullable |
| `address` | `text` |  Nullable |
| `city` | `varchar` |  Nullable |
| `state` | `varchar` |  Nullable |
| `pincode` | `varchar` |  Nullable |
| `gst_number` | `varchar` |  Nullable |
| `pan_number` | `varchar` |  Nullable |
| `bank_name` | `varchar` |  Nullable |
| `bank_account_number` | `varchar` |  Nullable |
| `bank_ifsc` | `varchar` |  Nullable |
| `payment_terms` | `int4` |  Nullable |
| `credit_limit` | `numeric` |  Nullable |
| `is_active` | `bool` |  Nullable |
| `notes` | `text` |  Nullable |
| `created_at` | `timestamptz` |  Nullable |
| `updated_at` | `timestamptz` |  Nullable |

## Table `waitlist_entries`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `branch_id` | `uuid` |  Nullable |
| `user_id` | `text` |  |
| `customer_name` | `varchar` |  |
| `phone` | `varchar` |  Nullable |
| `party_size` | `int4` |  |
| `source` | `varchar` |  |
| `status` | `varchar` |  |
| `position` | `int4` |  |
| `estimated_wait_minutes` | `int4` |  Nullable |
| `notes` | `text` |  Nullable |
| `notified_at` | `timestamptz` |  Nullable |
| `expires_at` | `timestamptz` |  Nullable |
| `seated_at` | `timestamptz` |  Nullable |
| `assigned_table_id` | `uuid` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `waitlist_history`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  |
| `waitlist_entry_id` | `uuid` |  |
| `action` | `varchar` |  |
| `details` | `jsonb` |  Nullable |
| `performed_by` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |

## Table `waitlist_settings`

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `restaurant_id` | `uuid` |  Unique |
| `user_id` | `text` |  |
| `notify_expiry_minutes` | `int4` |  |
| `avg_turnover_minutes` | `int4` |  |
| `sms_enabled` | `bool` |  |
| `whatsapp_enabled` | `bool` |  |
| `display_screen_enabled` | `bool` |  |
| `qr_entry_enabled` | `bool` |  |
| `auto_notify` | `bool` |  |
| `best_fit_enabled` | `bool` |  |
| `display_message` | `text` |  Nullable |
| `created_at` | `timestamptz` |  |
| `updated_at` | `timestamptz` |  |

## Table `webhook_events`

Durable webhook ledger.  Every callback from a payment gateway is inserted
     here before being applied to payments/orders.  Provides:
       * replay-safe idempotency surviving Redis flushes
       * complete forensic audit trail
       * basis for the "webhook delay/failure" reconciliation check.

### Columns

| Name | Type | Constraints |
|------|------|-------------|
| `id` | `uuid` | Primary |
| `gateway` | `varchar` |  |
| `event_type` | `varchar` |  |
| `event_id` | `varchar` |  Nullable |
| `gateway_payment_id` | `varchar` |  Nullable |
| `gateway_order_id` | `varchar` |  Nullable |
| `user_id` | `text` |  Nullable |
| `restaurant_id` | `uuid` |  Nullable |
| `branch_id` | `uuid` |  Nullable |
| `payment_id` | `uuid` |  Nullable |
| `order_id` | `uuid` |  Nullable |
| `raw_payload` | `jsonb` |  |
| `signature` | `text` |  Nullable |
| `signature_valid` | `bool` |  |
| `status` | `varchar` |  |
| `error_message` | `text` |  Nullable |
| `attempts` | `int4` |  |
| `received_at` | `timestamptz` |  |
| `processed_at` | `timestamptz` |  Nullable |

