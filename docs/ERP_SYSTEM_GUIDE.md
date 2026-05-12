# ERP System — Architecture, Data Flow & Examples

## System Overview

Migration `006_erp_full_system.sql` transforms the POS into a restaurant ERP with:

| Module | Tables | Purpose |
|--------|--------|---------|
| **Double-Entry Accounting** | `chart_of_accounts`, `journal_entries`, `journal_lines` | Every ₹ traceable |
| **Recipes** | `recipes`, `recipe_ingredients` | Formalized item → ingredient mapping |
| **Inventory Ledger** | `inventory_ledger` | Append-only, stock = SUM(in - out) |
| **Vendors & GRN** | `vendors`, `goods_receipt_notes`, `grn_items`, `vendor_payments` | Full procurement |
| **Cash Control** | `cash_drawers`, `shifts`, `shift_transactions` | Shift-level cash accountability |
| **Stock Transfers** | `stock_transfers`, `stock_transfer_items` | Inter-branch movement |
| **GST Compliance** | `tax_rates`, `item_tax_mapping`, `order_tax_details`, `gst_invoice_items`, `gst_reports` | Filing-ready |
| **Analytics** | `item_profitability`, `daily_pnl` | Real-time P&L |

---

## Performance Architecture

```
         HOT PATH (< 50ms)               ASYNC (Event-Driven)
    ┌────────────────────┐          ┌──────────────────────────┐
    │  POST /orders      │─event──▶ │ inventory_ledger INSERT  │
    │  POST /payments    │─event──▶ │ journal_entries INSERT   │
    │                    │          │ order_tax_details INSERT │
    │  (orders, payments,│          │ daily_pnl UPDATE        │
    │   kitchen_orders)  │          │ item_profitability       │
    └────────────────────┘          └──────────────────────────┘
           │                                    │
           ▼                                    ▼
    ┌──────────────┐                 ┌──────────────────────┐
    │  PostgreSQL   │                │  Same PostgreSQL      │
    │  (POS tables) │                │  (ERP tables)         │
    └──────────────┘                 └──────────────────────┘
```

**Rule**: POS inserts/updates NEVER join ERP tables. ERP writes happen in event handlers only.

---

## Data Flow: Order → Inventory → Accounting → Analytics

### 1. Order Placed (₹500 + 5% GST = ₹525)

**Event: `ORDER_CONFIRMED`**

```
ORDER_CONFIRMED
  ├─▶ InventoryService.deduct_for_order()     [existing — ingredients + inventory_transactions]
  ├─▶ inventory_ledger INSERT (consumption)    [new — append-only ledger]
  ├─▶ orders.cost_of_goods_sold = ₹180        [new — COGS snapshot]
  └─▶ journal_entries + journal_lines          [new — COGS journal]
        DR: 5001 (COGS - Food)         ₹180
        CR: 1004 (Inventory - Food)    ₹180
```

### 2. Payment Received (₹525 cash)

**Event: `PAYMENT_COMPLETED`**

```
PAYMENT_COMPLETED
  ├─▶ accounting_entries INSERT (revenue)      [existing — backward compat]
  └─▶ journal_entries + journal_lines          [new — double-entry with GST]
        DR: 1001 (Cash)                ₹525.00
        CR: 4001 (Food Sales)          ₹500.00
        CR: 2002 (CGST Payable)        ₹ 12.50
        CR: 2003 (SGST Payable)        ₹ 12.50
```

### 3. Analytics Update (async aggregation)

```
fn_aggregate_daily_pnl(restaurant_id, branch_id, date)
  → daily_pnl:
      revenue:       ₹500
      cogs:          ₹180
      gross_profit:  ₹320
      margin:        64%
```

---

## Complete Example: Order ₹500

### Input
- **Butter Chicken** × 1 @ ₹350 (5% GST inclusive)
- **Garlic Naan** × 2 @ ₹75 each (5% GST inclusive)

### Recipe (Butter Chicken)
| Ingredient | Qty Required | Cost/Unit | Line Cost |
|-----------|-------------|-----------|-----------|
| Chicken | 250g | ₹0.32/g | ₹80.00 |
| Butter | 50g | ₹0.60/g | ₹30.00 |
| Tomatoes | 200g | ₹0.08/g | ₹16.00 |
| Spice Mix | 15g | ₹1.20/g | ₹18.00 |

### Recipe (Garlic Naan × 2)
| Ingredient | Qty Required | Cost/Unit | Line Cost |
|-----------|-------------|-----------|-----------|
| Flour | 200g | ₹0.05/g | ₹10.00 |
| Butter | 30g | ₹0.60/g | ₹18.00 |
| Garlic | 20g | ₹0.40/g | ₹8.00 |

### Step 1: Inventory Deduction

**inventory_ledger entries:**

| ingredient_id | type | qty_out | unit_cost | reference |
|--------------|------|---------|-----------|-----------|
| chicken | consumption | 250 | 0.32 | order:{id} |
| butter | consumption | 80 | 0.60 | order:{id} |
| tomatoes | consumption | 200 | 0.08 | order:{id} |
| spice_mix | consumption | 15 | 1.20 | order:{id} |
| flour | consumption | 200 | 0.05 | order:{id} |
| garlic | consumption | 20 | 0.40 | order:{id} |

**Total COGS: ₹180.00** → saved to `orders.cost_of_goods_sold`

### Step 2: COGS Journal Entry

| Account | Debit | Credit |
|---------|-------|--------|
| 5001 COGS - Food | ₹180.00 | |
| 1004 Inventory - Food | | ₹180.00 |

### Step 3: Tax Calculation (5% GST, Inclusive)

```
Butter Chicken ₹350 inclusive:
  taxable = 350 / 1.05 = ₹333.33
  CGST = 333.33 × 2.5% = ₹8.33
  SGST = 333.33 × 2.5% = ₹8.33

Garlic Naan ₹150 inclusive:
  taxable = 150 / 1.05 = ₹142.86
  CGST = 142.86 × 2.5% = ₹3.57
  SGST = 142.86 × 2.5% = ₹3.57
```

**order_tax_details:**

| taxable_amount | cgst | sgst | total_tax |
|---------------|------|------|-----------|
| ₹476.19 | ₹11.90 | ₹11.91 | ₹23.81 |

### Step 4: Payment Journal Entry (Cash ₹500)

| Account | Debit | Credit |
|---------|-------|--------|
| 1001 Cash | ₹500.00 | |
| 4001 Food Sales | | ₹476.19 |
| 2002 CGST Payable | | ₹11.90 |
| 2003 SGST Payable | | ₹11.91 |

### Step 5: Daily P&L

| Metric | Amount |
|--------|--------|
| Revenue | ₹476.19 |
| COGS | ₹180.00 |
| Gross Profit | ₹296.19 |
| Gross Margin | 62.2% |

---

## Procurement Flow: Purchase → GRN → Inventory → Accounting

```
1. CREATE purchase_order (vendor_id, items, quantities)
     status: draft → approved

2. CREATE goods_receipt_note (purchase_order_id)
     + grn_items (received_quantity, unit_cost)
     status: draft → verified

3. EVENT: GRN_VERIFIED
     ├─▶ inventory_ledger INSERT (purchase IN)
     ├─▶ ingredients.current_stock += received_qty
     └─▶ journal_entries:
           DR: 1004 (Inventory)       ₹5,000
           CR: 2001 (Accounts Payable) ₹5,000

4. CREATE vendor_payment (vendor_id, amount, method)
     └─▶ journal_entries:
           DR: 2001 (Accounts Payable) ₹5,000
           CR: 1001/1002 (Cash/Bank)   ₹5,000
```

---

## Cash Control: Shift Flow

```
1. OPEN SHIFT (user_id, branch_id, opening_cash: ₹5,000)
     → shifts.status = 'open'

2. DURING SHIFT:
     Each sale/refund/expense → shift_transactions

3. CLOSE SHIFT (closing_cash: ₹12,300)
     expected_cash = opening + cash_sales - cash_refunds - cash_expenses
     cash_difference = closing - expected
     → Flag if |difference| > threshold
```

---

## Inter-Branch Transfer

```
1. Branch A creates stock_transfer (to Branch B)
     + stock_transfer_items (ingredient, qty)
     status: draft → approved → in_transit

2. Branch A ships:
     inventory_ledger: transfer_out from Branch A

3. Branch B receives:
     inventory_ledger: transfer_in to Branch B
     stock_transfer.status → received
```

---

## GST Report Generation

```sql
-- Generate GSTR-1 for a month
SELECT fn_generate_gst_report(
    'restaurant-uuid',
    'GSTR1',
    '2026-04-01',
    '2026-04-30'
);

-- View tax liability
SELECT * FROM gst_reports
WHERE restaurant_id = 'restaurant-uuid'
  AND report_type = 'tax_liability';
```

---

## Chart of Accounts (Default)

| Code | Name | Type |
|------|------|------|
| **1000** | **Assets** | asset |
| 1001 | Cash | asset |
| 1002 | Bank Account | asset |
| 1003 | Accounts Receivable | asset |
| 1004 | Inventory - Food | asset |
| 1005 | Inventory - Beverages | asset |
| **2000** | **Liabilities** | liability |
| 2001 | Accounts Payable | liability |
| 2002 | CGST Payable | liability |
| 2003 | SGST Payable | liability |
| 2004 | IGST Payable | liability |
| **3000** | **Equity** | equity |
| 3001 | Owner Capital | equity |
| 3002 | Retained Earnings | equity |
| **4000** | **Revenue** | revenue |
| 4001 | Food Sales | revenue |
| 4002 | Beverage Sales | revenue |
| **5000** | **Expenses** | expense |
| 5001 | COGS - Food | expense |
| 5002 | COGS - Beverages | expense |
| 5003-5010 | Operating Expenses | expense |

---

## Recommended Execution Order

| Phase | What | Why First |
|-------|------|-----------|
| **1** | Recipe + Inventory Ledger | Immediate ROI: know real cost per dish |
| **2** | Purchase → GRN → Inventory | Automate procurement pipeline |
| **3** | Basic P&L (daily_pnl) | Owner sees profit without full accounting |
| **4** | GST Tax System | Compliance, accurate invoicing |
| **5** | Double-Entry Accounting | Full financial audit trail |
| **6** | Cash Control + Shifts | Fraud detection, accountability |
| **7** | Inter-Branch Transfers | Only if multi-branch |
| **8** | Full Analytics + Profitability | Data-driven menu optimization |

---

## Useful SQL Queries

```sql
-- Current stock per ingredient (from ledger)
SELECT * FROM v_ingredient_stock_ledger
WHERE restaurant_id = 'xxx' AND branch_id = 'yyy';

-- Account balances (trial balance)
SELECT * FROM v_trial_balance WHERE restaurant_id = 'xxx';

-- Vendor outstanding
SELECT * FROM v_vendor_balances WHERE restaurant_id = 'xxx';

-- Item COGS calculation
SELECT fn_calculate_item_cogs(item_id, 1) FROM items WHERE "Item_ID" = 42;

-- Tax calculation
SELECT * FROM fn_calculate_tax(350.00, 'tax-rate-uuid', false);

-- Daily P&L
SELECT fn_aggregate_daily_pnl('restaurant-uuid', 'branch-uuid', CURRENT_DATE);
```

---

## Backward Compatibility

| Existing System | Still Works? | ERP Addition |
|----------------|-------------|--------------|
| `accounting_entries` (single-entry) | ✅ Unchanged | `journal_entries` + `journal_lines` added in parallel |
| `inventory_transactions` | ✅ Unchanged | `inventory_ledger` added in parallel |
| `item_ingredients` | ✅ Unchanged | `recipes` + `recipe_ingredients` (fallback to item_ingredients) |
| `ingredients.current_stock` | ✅ Still updated | Ledger is source of truth; current_stock remains cache |
| `invoices` (basic) | ✅ Unchanged | New GST columns are all nullable/defaulted |
| `purchase_orders` | ✅ Unchanged | New `vendor_id` + `restaurant_id` columns are nullable |
| `orders` | ✅ Unchanged | New `cost_of_goods_sold` + `order_type` columns are defaulted |

**Zero breaking changes**. All existing APIs, services, and queries continue to work.
