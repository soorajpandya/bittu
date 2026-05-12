# ERP Upgrade Guide — New Endpoints & Flutter Instructions

> **Base URL**: `https://api.bittupos.com/api/v1`
> **Auth**: Bearer token (Supabase JWT) in `Authorization` header
> **Migration 007** must be run in Supabase SQL Editor BEFORE using new endpoints.
> After migration, call the 3 seed endpoints to initialize each restaurant.

---

## STEP 1: Run Migration

Copy the contents of `migrations/007_erp_connect_harden.sql` and execute in **Supabase SQL Editor**.

## STEP 2: Seed Default Data

Call these once per restaurant (owner role required):

```
POST /erp/seed/feature-flags
POST /erp/seed/chart-of-accounts
POST /erp/seed/tax-rates
```

All return `{"status": "seeded"}`. Safe to call multiple times (idempotent).

---

## New Endpoints Reference

### 1. Tax Rules (Dynamic Rule Engine)

Priority-based tax rule matching. Resolves the correct GST rate based on order type, platform, interstate, category, amount, time.

#### `GET /erp/tax-rules?is_active=true`

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "Swiggy Delivery GST 5%",
    "priority": 10,
    "tax_rate_id": "uuid",
    "tax_rate_name": "GST 5%",
    "rate_percentage": 5.0,
    "order_type": "delivery",
    "platform": "swiggy",
    "is_interstate": null,
    "applicable_on": "food",
    "min_order_value": null,
    "max_order_value": null,
    "time_from": null,
    "time_to": null,
    "is_active": true,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

#### `POST /erp/tax-rules`

**Body:**
```json
{
  "name": "Interstate Delivery GST 18%",
  "priority": 20,
  "tax_rate_id": "uuid-of-tax-rate",
  "order_type": "delivery",
  "platform": null,
  "is_interstate": true,
  "applicable_on": "alcohol",
  "min_order_value": null,
  "max_order_value": null,
  "time_from": null,
  "time_to": null
}
```

Field reference:
- `priority`: Lower number = higher priority. Recommend 10, 20, 30, etc.
- `order_type`: `dine_in`, `takeaway`, `delivery`, or `null` (match any)
- `platform`: `swiggy`, `zomato`, `direct`, or `null` (match any)
- `applicable_on`: `food`, `beverage`, `alcohol`, `service`, `combo`, or `null` (match any)
- `time_from`/`time_to`: `"HH:MM:SS"` format for time-based rules (e.g. happy hour)

#### `PATCH /erp/tax-rules/{rule_id}`

Same body as POST. Updates existing rule.

#### `DELETE /erp/tax-rules/{rule_id}`

Soft-deletes (sets `is_active=false`). Returns `{"status": "deactivated"}`.

---

### 2. Platform Tax Config

Configure how delivery platforms (Swiggy, Zomato, Magicpin) handle GST.
When `gst_handled_by_platform=true`, the system marks orders from that platform as externally taxed and skips auto GST invoice creation.

#### `GET /erp/platform-tax-config`

**Response:**
```json
[
  {
    "id": "uuid",
    "platform": "swiggy",
    "gst_handled_by_platform": true,
    "commission_rate": 22.5,
    "tcs_rate": 1.0,
    "notes": "Swiggy collects and remits GST",
    "is_active": true,
    "created_at": "...",
    "updated_at": "..."
  }
]
```

#### `POST /erp/platform-tax-config`

**Body:**
```json
{
  "platform": "zomato",
  "gst_handled_by_platform": true,
  "commission_rate": 20.0,
  "tcs_rate": 1.0,
  "notes": "Zomato handles GST for all orders"
}
```

Upserts — if platform already exists for this restaurant, it updates.

#### `PATCH /erp/platform-tax-config/{config_id}`

Same body as POST.

---

### 3. Feature Flags

Control ERP behavior per restaurant. Seed first with `POST /erp/seed/feature-flags`.

#### `GET /erp/feature-flags`

**Response:**
```json
[
  {"flag_name": "erp.auto_gst_invoice", "is_enabled": true, "metadata": {"description": "Auto-create GST invoice on order"}, "updated_at": "..."},
  {"flag_name": "erp.auto_inventory_deduction", "is_enabled": true, "metadata": {"description": "Auto-deduct inventory on order confirm"}, "updated_at": "..."},
  {"flag_name": "erp.auto_journal_entries", "is_enabled": true, "metadata": {"description": "Auto-create journal entries on payment"}, "updated_at": "..."},
  {"flag_name": "erp.daily_pnl_auto_aggregate", "is_enabled": true, "metadata": {"description": "Auto-aggregate daily P&L on shift close"}, "updated_at": "..."},
  {"flag_name": "erp.e_invoice", "is_enabled": false, "metadata": {"description": "Enable e-Invoice generation (IRN)"}, "updated_at": "..."},
  {"flag_name": "erp.platform_tax_handling", "is_enabled": false, "metadata": {"description": "Enable platform-specific GST handling"}, "updated_at": "..."},
  {"flag_name": "erp.tax_rule_engine", "is_enabled": false, "metadata": {"description": "Use dynamic tax rules instead of item_tax_mapping"}, "updated_at": "..."}
]
```

#### `PATCH /erp/feature-flags/{flag_name}`

**Body:** `{"is_enabled": true}`

**Example:** `PATCH /erp/feature-flags/erp.tax_rule_engine`

---

### 4. Consistency Check

Validates ERP data integrity: inventory stock vs ledger, journal balance, order tax vs details.

#### `GET /erp/consistency-check`

**Response (healthy):**
```json
{
  "restaurant_id": "uuid",
  "total_issues": 0,
  "status": "healthy",
  "issues": []
}
```

**Response (with issues):**
```json
{
  "restaurant_id": "uuid",
  "total_issues": 2,
  "status": "issues_found",
  "issues": [
    {
      "check_name": "inventory_stock_vs_ledger",
      "status": "MISMATCH",
      "expected_value": 100.0,
      "actual_value": 95.5,
      "difference": -4.5
    },
    {
      "check_name": "journal_balance",
      "status": "IMBALANCED",
      "expected_value": 5000.0,
      "actual_value": 4800.0,
      "difference": 200.0
    }
  ]
}
```

Check names: `inventory_stock_vs_ledger`, `journal_balance`, `order_tax_vs_details`.

---

### 5. ERP Event Log

Audit trail of every ERP event processed (inventory deductions, journal entries, GST invoices, etc).

#### `GET /erp/event-log?event_type=ORDER_CONFIRMED&status=completed&limit=50&offset=0`

All query params optional.

**Response:**
```json
[
  {
    "id": "uuid",
    "event_type": "ORDER_CONFIRMED",
    "reference_type": "order",
    "reference_id": "order-uuid",
    "status": "completed",
    "error_message": null,
    "processing_time_ms": 45,
    "created_at": "..."
  }
]
```

Status values: `completed`, `failed`, `skipped`.

---

### 6. Order ERP Summary

Full financial view of a single order with tax breakdown and linked invoice.

#### `GET /erp/order-summary/{order_id}`

**Response:**
```json
{
  "order_id": "uuid",
  "restaurant_id": "uuid",
  "branch_id": "uuid",
  "order_type": "dine_in",
  "platform": "direct",
  "subtotal": 1000.0,
  "tax_amount": 50.0,
  "discount_amount": 0,
  "total_amount": 1050.0,
  "cost_of_goods_sold": 350.0,
  "gross_profit": 700.0,
  "margin_percent": 66.7,
  "gst_handled_externally": false,
  "status": "completed",
  "created_at": "...",
  "tax_breakdown": [
    {
      "tax_name": "GST 5%",
      "rate_percentage": 5.0,
      "taxable_amount": 1000.0,
      "cgst_amount": 25.0,
      "sgst_amount": 25.0,
      "igst_amount": 0,
      "total_tax": 50.0
    }
  ],
  "invoice": {
    "id": 123,
    "invoice_number": "INV-00001",
    "taxable_amount": 1000.0,
    "cgst_amount": 25.0,
    "sgst_amount": 25.0,
    "igst_amount": 0,
    "total_amount": 1050.0
  }
}
```

---

### 7. Inventory Ledger Summary (Updated)

`GET /erp/inventory-ledger/summary` now returns **real** reorder point data:

- `reorder_point`: From `ingredients.reorder_level` (set via ingredient management)
- `is_low`: `true` when `current_stock < reorder_level` and `reorder_level > 0`
- **New filter**: `?low_only=true` — returns only items below reorder level

---

## Flutter UI Screens to Build

### Screen 1: ERP Settings Dashboard
Top-level ERP configuration screen with:
- **Feature Flags** — list of toggles (Switch widgets)
- **Seed Data** — 3 buttons (feature flags, chart of accounts, tax rates)
- **Consistency Check** — button + results display
- Navigation to Tax Rules, Platform Config, Event Log

### Screen 2: Tax Rules Management
- List all tax rules sorted by priority
- FAB to create new rule
- Swipe to deactivate
- Form: name, priority (number), tax rate (dropdown from `/erp/tax-rates`), order type (dropdown), platform (dropdown), interstate (checkbox), applicable_on (dropdown), min/max order value, time range

### Screen 3: Platform Tax Config
- List platforms (Swiggy, Zomato, Magicpin)
- Toggle: "GST handled by platform"
- Fields: commission rate %, TCS rate %, notes
- Show/hide based on `erp.platform_tax_handling` feature flag

### Screen 4: Feature Flags
- Simple list with Switch toggle for each flag
- Show description from `metadata.description`
- Show last updated timestamp

### Screen 5: ERP Event Log
- Filterable list (by event type, status)
- Paginated (limit/offset)
- Color-code: green=completed, red=failed, grey=skipped
- Show processing_time_ms

### Screen 6: Consistency Check Dashboard
- Big button: "Run Consistency Check"
- Status badge: healthy (green) / issues_found (red)
- Table of issues with check_name, expected, actual, difference

### Screen 7: Order ERP Detail
Add "Financial Details" section to existing order detail:
- Gross profit & margin %
- Tax breakdown table
- Linked invoice number
- GST handled externally badge

---

## Dart Data Models

```dart
class TaxRule {
  final String id;
  final String name;
  final int priority;
  final String taxRateId;
  final String? taxRateName;
  final double? ratePercentage;
  final String? orderType;
  final String? platform;
  final bool? isInterstate;
  final String? applicableOn;
  final double? minOrderValue;
  final double? maxOrderValue;
  final String? timeFrom;
  final String? timeTo;
  final bool isActive;
}

class PlatformTaxConfig {
  final String id;
  final String platform;
  final bool gstHandledByPlatform;
  final double commissionRate;
  final double tcsRate;
  final String? notes;
}

class FeatureFlag {
  final String flagName;
  final bool isEnabled;
  final Map<String, dynamic>? metadata;
  final DateTime? updatedAt;
}

class ConsistencyCheckResult {
  final String restaurantId;
  final int totalIssues;
  final String status; // "healthy" or "issues_found"
  final List<ConsistencyIssue> issues;
}

class ConsistencyIssue {
  final String checkName;
  final String status;
  final double expectedValue;
  final double actualValue;
  final double difference;
}

class ErpEventLog {
  final String id;
  final String eventType;
  final String? referenceType;
  final String? referenceId;
  final String status;
  final String? errorMessage;
  final int? processingTimeMs;
  final DateTime createdAt;
}

class OrderErpSummary {
  final String orderId;
  final String orderType;
  final String platform;
  final double subtotal;
  final double taxAmount;
  final double discountAmount;
  final double totalAmount;
  final double costOfGoodsSold;
  final double grossProfit;
  final double marginPercent;
  final bool gstHandledExternally;
  final String status;
  final List<TaxBreakdownItem> taxBreakdown;
  final InvoiceSummary? invoice;
}

class TaxBreakdownItem {
  final String taxName;
  final double ratePercentage;
  final double taxableAmount;
  final double cgstAmount;
  final double sgstAmount;
  final double igstAmount;
  final double totalTax;
}

class InvoiceSummary {
  final int id;
  final String invoiceNumber;
  final double taxableAmount;
  final double cgstAmount;
  final double sgstAmount;
  final double igstAmount;
  final double totalAmount;
}
```

---

## Architecture: How It Works Behind the Scenes

### Event-Driven Flow (automatic, no frontend action needed)
1. POS creates order → `ORDER_CONFIRMED` event fires
2. If `erp.auto_inventory_deduction` ON → inventory auto-deducted via recipes
3. If `erp.auto_gst_invoice` ON → GST invoice auto-created from order items
4. Payment completes → `PAYMENT_COMPLETED` event fires
5. If `erp.auto_journal_entries` ON → double-entry journal auto-created
6. Shift closes → `SHIFT_CLOSED` event fires
7. If `erp.daily_pnl_auto_aggregate` ON → daily P&L recalculated

### Tax Resolution Priority (when `erp.tax_rule_engine` is ON)
1. **Tax Rules** (by priority) — most specific match wins
2. **Item Tax Mapping** — fallback per-item tax
3. **Restaurant Default** — GST 5% fallback

When OFF: Uses `item_tax_mapping` directly (existing behavior).

### Platform Tax Flow (when `erp.platform_tax_handling` is ON)
- Orders from platforms where `gst_handled_by_platform=true` → marked `gst_handled_externally=true`
- No GST invoice auto-generated for these orders
- Revenue still recorded in accounting
