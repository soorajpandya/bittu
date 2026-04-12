# ERP Frontend Implementation Guide

**Base URL:** `https://api.bittupos.com/api/v1`  
**Auth:** All requests require `Authorization: Bearer <supabase_jwt>`  
**Roles:** `owner`, `manager` (all ERP endpoints)

---

## 1. Accounting Dashboard

### 1.1 Cash Flow Summary Card

**API:** `GET /accounting/cash-flow`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `start_date` | `YYYY-MM-DD` | 30 days ago | |
| `end_date` | `YYYY-MM-DD` | today | |
| `branch_id` | `string?` | user's branch | optional override |

**Response:**
```json
{
  "total_revenue": 125000.00,
  "total_expenses": 45000.00,
  "total_refunds": 2500.00,
  "net_cash_flow": 77500.00,
  "period": { "start": "2026-03-12", "end": "2026-04-11" }
}
```

**Flutter widget:** 4-stat summary card at the top of Accounting screen.
```
┌──────────────────────────────────────────┐
│  Revenue        Expenses      Refunds    │
│  ₹1,25,000      ₹45,000       ₹2,500    │
│                                          │
│  Net Cash Flow: ₹77,500   ▲ (trend)     │
│  Period: Mar 12 – Apr 11                 │
└──────────────────────────────────────────┘
```

Date range picker at top — call API again when range changes.

---

### 1.2 Daily Breakdown Chart

**API:** `GET /accounting/daily-breakdown`

| Param | Type | Default |
|-------|------|---------|
| `start_date` | `YYYY-MM-DD` | 30 days ago |
| `end_date` | `YYYY-MM-DD` | today |
| `branch_id` | `string?` | user's branch |

**Response:**
```json
[
  { "date": "2026-04-10", "revenue": 8500.0, "expenses": 3200.0, "refunds": 0.0, "net": 5300.0 },
  { "date": "2026-04-11", "revenue": 9200.0, "expenses": 2800.0, "refunds": 500.0, "net": 5900.0 }
]
```

**Flutter widget:** Bar chart (use `fl_chart` or `syncfusion_flutter_charts`).
- Green bars for revenue, red bars for expenses
- Line overlay for net
- X-axis: dates, Y-axis: ₹ amount
- Tap a bar to see that day's entries

---

### 1.3 Payment Method Breakdown (Pie/Donut Chart)

**API:** `GET /accounting/payment-methods`

| Param | Type | Default |
|-------|------|---------|
| `start_date` | `YYYY-MM-DD` | 30 days ago |
| `end_date` | `YYYY-MM-DD` | today |
| `branch_id` | `string?` | user's branch |

**Response:**
```json
[
  { "method": "cash", "total": 55000.0 },
  { "method": "razorpay", "total": 45000.0 },
  { "method": "phonepe", "total": 25000.0 }
]
```

**Flutter widget:** Donut chart with legend. Colors per method:
- `cash` → green
- `razorpay` → blue
- `phonepe` → purple
- `upi` → orange
- `card` → teal
- `unknown` → grey

---

### 1.4 Accounting Entries List

**API:** `GET /accounting/entries`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `entry_type` | `revenue` / `expense` / `refund` | all | filter |
| `start_date` | `YYYY-MM-DD` | none | |
| `end_date` | `YYYY-MM-DD` | none | |
| `branch_id` | `string?` | user's branch | |
| `limit` | `int` | 50 | max 200 |
| `offset` | `int` | 0 | pagination |

**Response:**
```json
[
  {
    "id": "uuid",
    "user_id": "...",
    "restaurant_id": "...",
    "branch_id": "uuid",
    "entry_type": "revenue",
    "amount": 850.00,
    "payment_method": "cash",
    "category": null,
    "reference_type": "order",
    "reference_id": "ORD-20260411-001",
    "description": "Payment pay_xyz for order ORD-20260411-001",
    "created_at": "2026-04-11T13:00:00Z",
    "updated_at": "2026-04-11T13:00:00Z"
  }
]
```

**Flutter widget:** Scrollable list with filter chips.
```
┌──────────────────────────────────────────┐
│ [All] [Revenue] [Expense] [Refund]  🔽  │  ← filter chips + date picker
├──────────────────────────────────────────┤
│ ▲ ₹850.00    Order ORD-20260411-001     │  ← green arrow = revenue
│   cash · 11 Apr 1:00 PM                 │
├──────────────────────────────────────────┤
│ ▼ ₹3,200.00  Vegetables                 │  ← red arrow = expense
│   manual · 10 Apr 9:30 AM               │
├──────────────────────────────────────────┤
│ ▼ ₹500.00    Refund for ORD-20260410-05 │  ← orange arrow = refund
│   refund · 10 Apr 3:15 PM               │
└──────────────────────────────────────────┘
```

- Revenue: green `▲`, positive amount
- Expense: red `▼`, show absolute amount
- Refund: orange `▼`, show absolute amount
- Infinite scroll — increase `offset` by `limit` on each load
- Tap entry → detail bottomsheet

---

### 1.5 Record Expense (Manual)

**API:** `POST /accounting/expenses`

**Request body:**
```json
{
  "amount": 3200.00,
  "category": "Vegetables",
  "description": "Weekly vegetable purchase from Sabzi Mandi",
  "reference_type": "manual",
  "reference_id": null
}
```

**Response:** The created entry object (same shape as entries list item).

**Flutter widget:** Bottom sheet or dialog form.
```
┌── Record Expense ────────────────────────┐
│                                          │
│  Amount *        ₹ [________]            │
│                                          │
│  Category *      [Vegetables      ▼]     │  ← dropdown
│                                          │
│  Description     [__________________]    │
│                  [__________________]    │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Save Expense     │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

**Category suggestions** (hardcoded list):
`Vegetables`, `Groceries`, `Dairy`, `Meat`, `Packaging`, `Rent`, `Salary`, `Electricity`, `Gas`, `Maintenance`, `Marketing`, `Delivery`, `Miscellaneous`

---

## 2. AI Ingredient Mapping

### 2.1 Suggest Ingredients (Preview Only)

**API:** `POST /ai-ingredients/suggest`

**Request body:**
```json
{
  "item_name": "Paneer Butter Masala"
}
```

**Response:**
```json
[
  { "name": "Paneer", "quantity": 200, "unit": "g" },
  { "name": "Butter", "quantity": 30, "unit": "g" },
  { "name": "Tomato", "quantity": 150, "unit": "g" },
  { "name": "Cream", "quantity": 50, "unit": "ml" },
  { "name": "Onion", "quantity": 100, "unit": "g" },
  { "name": "Ginger Garlic Paste", "quantity": 15, "unit": "g" },
  { "name": "Kashmiri Red Chilli Powder", "quantity": 5, "unit": "g" },
  { "name": "Garam Masala", "quantity": 3, "unit": "g" },
  { "name": "Salt", "quantity": 5, "unit": "g" },
  { "name": "Refined Oil", "quantity": 20, "unit": "ml" },
  { "name": "Kasuri Methi", "quantity": 2, "unit": "g" },
  { "name": "Sugar", "quantity": 3, "unit": "g" }
]
```

> **Note:** This does NOT save anything. Use it as a preview before auto-linking.

---

### 2.2 Auto-Link Ingredients (Save)

**API:** `POST /ai-ingredients/auto-link`

**Request body:**
```json
{
  "item_id": 42,
  "item_name": "Paneer Butter Masala"
}
```

**Response:**
```json
[
  { "ingredient_id": "abc-123", "name": "Paneer", "quantity": 200, "unit": "g", "action": "linked" },
  { "ingredient_id": "def-456", "name": "Butter", "quantity": 30, "unit": "g", "action": "linked" },
  { "ingredient_id": "ghi-789", "name": "Tomato", "quantity": 150, "unit": "g", "action": "exists" }
]
```

- `action: "linked"` = newly created linkage
- `action: "exists"` = linkage already existed (skipped)

---

### 2.3 UI Flow for AI Ingredients

Place a **"🤖 Auto-Map Ingredients"** button on the **Item Edit** screen, next to the manual ingredient list.

**Flow:**

```
┌── Edit Item: Paneer Butter Masala ───────┐
│                                          │
│  Price: ₹280    Category: Main Course    │
│                                          │
│  ── Ingredients ──────────────────────── │
│  (empty — no ingredients linked yet)     │
│                                          │
│  ┌──────────────────────────────────┐    │
│  │  🤖 Auto-Map Ingredients (AI)   │    │  ← Step 1: tap this
│  └──────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

**Step 1:** On tap → call `POST /ai-ingredients/suggest` with `item_name`.  
Show loading spinner ("AI is analyzing recipe...").

**Step 2:** Show preview bottomsheet:

```
┌── AI Suggested Ingredients ──────────────┐
│                                          │
│  ☑ Paneer .............. 200 g           │
│  ☑ Butter .............. 30 g            │
│  ☑ Tomato .............. 150 g           │
│  ☑ Cream ............... 50 ml           │
│  ☑ Onion ............... 100 g           │
│  ☑ Ginger Garlic Paste . 15 g           │
│  ☑ Kashmiri Red Chilli .. 5 g           │
│  ☑ Garam Masala ........ 3 g            │
│  ☑ Salt ................ 5 g            │
│  ☑ Refined Oil ......... 20 ml          │
│  ☑ Kasuri Methi ........ 2 g            │
│  ☑ Sugar ............... 3 g            │
│                                          │
│  Quantities are editable before saving   │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Link All (12)    │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

- Checkboxes to deselect unwanted ingredients
- Quantity fields are editable
- User can review before committing

**Step 3:** On "Link All" → call `POST /ai-ingredients/auto-link` with `item_id` + `item_name`.  
This creates missing raw materials AND links them to the item.

**Step 4:** Refresh the ingredient list on the item edit screen. Show success snackbar:  
`"12 ingredients linked via AI"`.

---

## 3. Event-Driven Features (Automatic — No Frontend Needed)

These happen automatically in the backend. The frontend just sees the results:

| Event | Side Effect | Where You See It |
|-------|-------------|-------------------|
| Order confirmed | Ingredient stock deducted | Inventory stock levels update |
| Order cancelled | Ingredient stock restored | Inventory stock levels revert |
| Payment completed | Revenue entry created | Accounting entries list |
| Payment refunded | Refund entry created | Accounting entries list |

**No frontend changes needed** for these — they fire on existing order/payment flows.

---

## 4. Navigation Structure

Add to the admin drawer/sidebar:

```
📊 Dashboard
📋 Orders
🍽️ Menu
   └── Items (add AI ingredient button per item)
📦 Inventory
💰 Accounting          ← NEW
   ├── Cash Flow       (default tab)
   ├── Entries         (list + filters)
   └── Record Expense  (FAB or tab)
📈 Analytics
⚙️ Settings
```

---

## 5. Dart Models

```dart
// accounting_entry.dart
class AccountingEntry {
  final String id;
  final String entryType; // revenue, expense, refund
  final double amount;
  final String? paymentMethod;
  final String? category;
  final String? referenceType;
  final String? referenceId;
  final String description;
  final DateTime createdAt;

  AccountingEntry.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      entryType = json['entry_type'],
      amount = (json['amount'] as num).toDouble(),
      paymentMethod = json['payment_method'],
      category = json['category'],
      referenceType = json['reference_type'],
      referenceId = json['reference_id'],
      description = json['description'] ?? '',
      createdAt = DateTime.parse(json['created_at']);
}

// cash_flow.dart
class CashFlow {
  final double totalRevenue;
  final double totalExpenses;
  final double totalRefunds;
  final double netCashFlow;
  final String startDate;
  final String endDate;

  CashFlow.fromJson(Map<String, dynamic> json)
    : totalRevenue = (json['total_revenue'] as num).toDouble(),
      totalExpenses = (json['total_expenses'] as num).toDouble(),
      totalRefunds = (json['total_refunds'] as num).toDouble(),
      netCashFlow = (json['net_cash_flow'] as num).toDouble(),
      startDate = json['period']['start'],
      endDate = json['period']['end'];
}

// daily_breakdown.dart
class DailyBreakdown {
  final String date;
  final double revenue;
  final double expenses;
  final double refunds;
  final double net;

  DailyBreakdown.fromJson(Map<String, dynamic> json)
    : date = json['date'],
      revenue = (json['revenue'] as num).toDouble(),
      expenses = (json['expenses'] as num).toDouble(),
      refunds = (json['refunds'] as num).toDouble(),
      net = (json['net'] as num).toDouble();
}

// ai_ingredient_suggestion.dart
class AIIngredientSuggestion {
  final String name;
  final double quantity;
  final String unit;

  AIIngredientSuggestion.fromJson(Map<String, dynamic> json)
    : name = json['name'],
      quantity = (json['quantity'] as num).toDouble(),
      unit = json['unit'];
}

// linked_ingredient.dart
class LinkedIngredient {
  final String ingredientId;
  final String name;
  final double quantity;
  final String unit;
  final String action; // "linked" or "exists"

  LinkedIngredient.fromJson(Map<String, dynamic> json)
    : ingredientId = json['ingredient_id'].toString(),
      name = json['name'],
      quantity = (json['quantity'] as num).toDouble(),
      unit = json['unit'],
      action = json['action'];
}
```

---

## 6. API Service (Dart)

```dart
class AccountingApi {
  final Dio _dio;
  AccountingApi(this._dio);

  Future<CashFlow> getCashFlow({String? startDate, String? endDate, String? branchId}) async {
    final resp = await _dio.get('/accounting/cash-flow', queryParameters: {
      if (startDate != null) 'start_date': startDate,
      if (endDate != null) 'end_date': endDate,
      if (branchId != null) 'branch_id': branchId,
    });
    return CashFlow.fromJson(resp.data);
  }

  Future<List<AccountingEntry>> getEntries({
    String? entryType, String? startDate, String? endDate,
    int limit = 50, int offset = 0,
  }) async {
    final resp = await _dio.get('/accounting/entries', queryParameters: {
      if (entryType != null) 'entry_type': entryType,
      if (startDate != null) 'start_date': startDate,
      if (endDate != null) 'end_date': endDate,
      'limit': limit, 'offset': offset,
    });
    return (resp.data as List).map((e) => AccountingEntry.fromJson(e)).toList();
  }

  Future<List<DailyBreakdown>> getDailyBreakdown({String? startDate, String? endDate}) async {
    final resp = await _dio.get('/accounting/daily-breakdown', queryParameters: {
      if (startDate != null) 'start_date': startDate,
      if (endDate != null) 'end_date': endDate,
    });
    return (resp.data as List).map((e) => DailyBreakdown.fromJson(e)).toList();
  }

  Future<List<Map<String, dynamic>>> getPaymentMethods({String? startDate, String? endDate}) async {
    final resp = await _dio.get('/accounting/payment-methods', queryParameters: {
      if (startDate != null) 'start_date': startDate,
      if (endDate != null) 'end_date': endDate,
    });
    return List<Map<String, dynamic>>.from(resp.data);
  }

  Future<AccountingEntry> recordExpense({
    required double amount, required String category,
    String description = '', String? referenceType, String? referenceId,
  }) async {
    final resp = await _dio.post('/accounting/expenses', data: {
      'amount': amount, 'category': category,
      'description': description,
      if (referenceType != null) 'reference_type': referenceType,
      if (referenceId != null) 'reference_id': referenceId,
    });
    return AccountingEntry.fromJson(resp.data);
  }
}

class AIIngredientApi {
  final Dio _dio;
  AIIngredientApi(this._dio);

  Future<List<AIIngredientSuggestion>> suggest(String itemName) async {
    final resp = await _dio.post('/ai-ingredients/suggest', data: {'item_name': itemName});
    return (resp.data as List).map((e) => AIIngredientSuggestion.fromJson(e)).toList();
  }

  Future<List<LinkedIngredient>> autoLink(int itemId, String itemName) async {
    final resp = await _dio.post('/ai-ingredients/auto-link', data: {
      'item_id': itemId, 'item_name': itemName,
    });
    return (resp.data as List).map((e) => LinkedIngredient.fromJson(e)).toList();
  }
}
```

---

## 7. Quick Checklist

- [ ] Run `migrations/004_erp_accounting.sql` in Supabase SQL Editor
- [ ] Accounting screen: cash flow card + date range picker
- [ ] Daily breakdown bar chart
- [ ] Payment method donut chart
- [ ] Entries list with filter chips (All / Revenue / Expense / Refund)
- [ ] Record Expense form (FAB or bottom sheet)
- [ ] "Auto-Map Ingredients" button on item edit screen
- [ ] AI suggestion preview bottom sheet with checkboxes
- [ ] Auto-link confirmation + ingredient list refresh
- [ ] Add "Accounting" to navigation drawer

---
---

# ERP Full System – Frontend Implementation Guide

> **Prerequisite:** Run `migrations/006_erp_full_system.sql` in Supabase SQL Editor.  
> These features complement everything above. The backend creates ERP data automatically via domain events — the frontend just reads/writes via these APIs.

**New API base paths:**

| Module | Base Path | Required Role |
|--------|-----------|---------------|
| Chart of Accounts | `/erp/accounts` | owner, manager |
| Journal Entries | `/erp/journals` | owner, manager |
| Recipes | `/erp/recipes` | owner, manager |
| Inventory Ledger | `/erp/inventory-ledger` | owner, manager |
| Vendors | `/erp/vendors` | owner, manager |
| Goods Receipt Notes | `/erp/grn` | owner, manager |
| Vendor Payments | `/erp/vendor-payments` | owner, manager |
| Cash Drawers & Shifts | `/erp/shifts` | owner, manager, cashier |
| Stock Transfers | `/erp/transfers` | owner, manager |
| Tax Rates (GST) | `/erp/tax-rates` | owner, manager |
| GST Invoices | `/erp/gst-invoices` | owner, manager |
| GST Reports | `/erp/gst-reports` | owner, manager |
| Item Profitability | `/erp/profitability` | owner, manager |
| Daily P&L | `/erp/pnl` | owner, manager |

**Auth:** All endpoints require `Authorization: Bearer <supabase_jwt>`.

---

## 8. Chart of Accounts (Double-Entry Engine)

The backend seeds default accounts on restaurant onboarding via `fn_seed_chart_of_accounts()`. Frontend shows the account tree and lets the owner add custom accounts.

### 8.1 List Accounts

**API:** `GET /erp/accounts`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `account_type` | `asset`/`liability`/`equity`/`revenue`/`expense` | all | filter |
| `is_active` | `bool` | true | |

**Response:**
```json
[
  {
    "id": "uuid",
    "account_code": "1001",
    "name": "Cash",
    "account_type": "asset",
    "parent_id": "uuid-of-1000",
    "description": null,
    "is_system": true,
    "is_active": true,
    "created_at": "2026-04-11T00:00:00Z"
  }
]
```

**Flutter widget:** Tree view grouped by type.
```
┌── Chart of Accounts ─────────────────────┐
│                                          │
│  ▶ Assets (1000)                         │
│    ├── 1001  Cash                  🔒    │  ← 🔒 = system (cannot delete)
│    ├── 1002  Bank Account          🔒    │
│    ├── 1003  Accounts Receivable   🔒    │
│    ├── 1004  Inventory - Food      🔒    │
│    └── 1006  Prepaid Expenses           │
│                                          │
│  ▶ Liabilities (2000)                    │
│    ├── 2001  Accounts Payable      🔒    │
│    ├── 2002  CGST Payable          🔒    │
│    ├── 2003  SGST Payable          🔒    │
│    └── 2004  IGST Payable          🔒    │
│                                          │
│  ▶ Revenue (4000)                        │
│  ▶ Expenses (5000)                       │
│  ▶ Equity (3000)                         │
│                                          │
│  ┌────────────────────────────────┐      │
│  │  + Add Custom Account          │      │
│  └────────────────────────────────┘      │
└──────────────────────────────────────────┘
```

- Group by `account_type`, sort by `account_code` within group
- Show 🔒 icon for `is_system: true` (cannot delete)
- Expand/collapse each type group

### 8.2 Create Custom Account

**API:** `POST /erp/accounts`

**Request body:**
```json
{
  "account_code": "5011",
  "name": "Kitchen Equipment Maintenance",
  "account_type": "expense",
  "parent_id": "uuid-of-5000",
  "description": "Monthly kitchen equipment servicing"
}
```

**Validation:**
- `account_code` must be unique within the restaurant
- `account_type` must match parent's type
- System accounts cannot be created via API

### 8.3 Account Balances (Trial Balance)

**API:** `GET /erp/accounts/balances`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `as_of_date` | `YYYY-MM-DD` | today | point-in-time balance |

**Response:**
```json
[
  {
    "account_code": "1001",
    "name": "Cash",
    "account_type": "asset",
    "total_debit": 125000.00,
    "total_credit": 43000.00,
    "balance": 82000.00
  }
]
```

**Flutter widget:** Trial balance table.
```
┌── Trial Balance (as of 11 Apr 2026) ────┐
│                                          │
│  Account           Debit     Credit      │
│  ──────────────────────────────────────  │
│  1001 Cash         ₹82,000              │
│  1002 Bank         ₹45,000              │
│  1004 Inventory    ₹28,000              │
│  2001 A/P                    ₹12,000    │
│  2002 CGST Payable           ₹3,500     │
│  2003 SGST Payable           ₹3,500     │
│  4001 Food Sales             ₹1,25,000  │
│  5001 COGS - Food  ₹31,000              │
│  ──────────────────────────────────────  │
│  TOTAL             ₹1,86,000 ₹1,44,000  │
│                                          │
│  ⚠️ Debit ≠ Credit → investigate!        │  ← Only show if mismatch
└──────────────────────────────────────────┘
```

- Filter by `account_type` tabs: All | Assets | Liabilities | Revenue | Expenses
- Debit = Credit means books are balanced (show green check ✅)
- Show warning only if they differ

---

## 9. Journal Entries (Double-Entry Transactions)

Most journals are created automatically by event handlers (order → payment → COGS). This screen is read-only for viewing financial transactions and allows manual adjustment entries.

### 9.1 List Journal Entries

**API:** `GET /erp/journals`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `reference_type` | `order`/`payment`/`refund`/`purchase`/`expense`/`grn`/`transfer`/`adjustment` | all | filter |
| `start_date` | `YYYY-MM-DD` | last 30 days | |
| `end_date` | `YYYY-MM-DD` | today | |
| `branch_id` | `string?` | user's branch | |
| `limit` | `int` | 50 | max 200 |
| `offset` | `int` | 0 | pagination |

**Response:**
```json
[
  {
    "id": "uuid",
    "entry_date": "2026-04-11",
    "reference_type": "payment",
    "reference_id": "pay_xyz",
    "description": "Payment for ORD-20260411-001",
    "is_reversed": false,
    "created_by": "user-uuid",
    "created_at": "2026-04-11T13:00:00Z",
    "lines": [
      { "account_code": "1001", "account_name": "Cash", "debit": 850.00, "credit": 0 },
      { "account_code": "4001", "account_name": "Food Sales", "debit": 0, "credit": 809.52 },
      { "account_code": "2002", "account_name": "CGST Payable", "debit": 0, "credit": 20.24 },
      { "account_code": "2003", "account_name": "SGST Payable", "debit": 0, "credit": 20.24 }
    ],
    "total_debit": 850.00,
    "total_credit": 850.00
  }
]
```

**Flutter widget:** List with expandable detail.
```
┌── Journal Entries ───────────────────────┐
│ [All] [Payment] [Order] [Refund]  📅     │  ← filter chips + date picker
├──────────────────────────────────────────┤
│ 📄 Payment for ORD-20260411-001          │
│    11 Apr · ₹850.00 · payment            │
│    ▼ Expand                              │
│  ┌────────────────────────────────────┐  │
│  │ 1001 Cash           DR ₹850.00    │  │
│  │ 4001 Food Sales     CR ₹809.52    │  │
│  │ 2002 CGST Payable   CR  ₹20.24    │  │
│  │ 2003 SGST Payable   CR  ₹20.24    │  │
│  │ ─────────────────────────────────  │  │
│  │ Total: DR ₹850.00 = CR ₹850.00 ✅ │  │
│  └────────────────────────────────────┘  │
├──────────────────────────────────────────┤
│ 📄 COGS for ORD-20260411-001            │
│    11 Apr · ₹180.00 · order              │
│    ▼ Expand                              │
└──────────────────────────────────────────┘
```

- **DR** in green highlight, **CR** in blue highlight
- Show ✅ when total DR = total CR (always should)
- Reversed entries: show strikethrough + "Reversed" badge
- Tap reference_id → navigate to order/payment detail

### 9.2 Create Manual Journal Entry

**API:** `POST /erp/journals`

**Request body:**
```json
{
  "entry_date": "2026-04-11",
  "reference_type": "adjustment",
  "description": "Correct inventory valuation",
  "lines": [
    { "account_code": "1004", "debit": 5000.00, "credit": 0, "description": "Add inventory value" },
    { "account_code": "3002", "debit": 0, "credit": 5000.00, "description": "Offset to retained earnings" }
  ]
}
```

**Validation (client-side):**
- Minimum 2 lines
- Total debit MUST equal total credit (show live balance indicator)
- Each line: either debit > 0 OR credit > 0, not both

**Flutter widget:** Dynamic form.
```
┌── New Journal Entry ─────────────────────┐
│                                          │
│  Date:  [11 Apr 2026]                    │
│  Type:  [Adjustment    ▼]               │
│  Desc:  [Correct inventory valuation  ]  │
│                                          │
│  ── Lines ────────────────────────────── │
│  Account         Debit      Credit       │
│  [1004 Invnt ▼]  [5,000]    [     ]     │
│  [3002 Retnd ▼]  [     ]    [5,000]     │
│                                          │
│  + Add Line                              │
│                                          │
│  ──────────────────────────────────────  │
│  Balance: DR ₹5,000 = CR ₹5,000  ✅     │  ← live, turn red if unequal
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Post Entry       │      │  ← disabled if unbalanced
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

### 9.3 Reverse Journal Entry

**API:** `POST /erp/journals/{journal_id}/reverse`

**Response:** Returns the new reversal journal entry.

- Show confirmation dialog: "This will create a reverse entry. Continue?"
- Original entry gets `is_reversed: true` and `reversed_by: new_entry_id`

---

## 10. Recipe Management

Recipes formalize the link between menu items and ingredients. They're separate from the legacy `item_ingredients` table for backwards compatibility.

### 10.1 List Recipes

**API:** `GET /erp/recipes`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `item_id` | `int?` | null | filter by menu item |
| `is_active` | `bool` | true | |

**Response:**
```json
[
  {
    "id": "uuid",
    "item_id": 42,
    "item_name": "Paneer Butter Masala",
    "name": "Paneer Butter Masala Recipe",
    "yield_quantity": 1,
    "yield_unit": "portion",
    "is_active": true,
    "total_cost": 82.50,
    "ingredients": [
      {
        "id": "uuid",
        "ingredient_id": "ing-001",
        "ingredient_name": "Paneer",
        "quantity_required": 200,
        "unit": "g",
        "waste_percent": 5,
        "unit_cost": 0.32,
        "line_cost": 67.20
      }
    ]
  }
]
```

### 10.2 Create/Update Recipe

**API:** `POST /erp/recipes` (create) / `PATCH /erp/recipes/{recipe_id}` (update)

**Request body:**
```json
{
  "item_id": 42,
  "name": "Paneer Butter Masala Recipe",
  "yield_quantity": 1,
  "yield_unit": "portion",
  "notes": "Standard recipe for 1 plate",
  "ingredients": [
    { "ingredient_id": "ing-001", "quantity_required": 200, "unit": "g", "waste_percent": 5 },
    { "ingredient_id": "ing-002", "quantity_required": 30, "unit": "g", "waste_percent": 0 },
    { "ingredient_id": "ing-003", "quantity_required": 150, "unit": "g", "waste_percent": 10 }
  ]
}
```

**Flutter widget:** Recipe editor.
```
┌── Recipe: Paneer Butter Masala ──────────┐
│                                          │
│  Menu Item:  Paneer Butter Masala (₹280) │
│  Yield:      [1] [portion ▼]            │
│                                          │
│  ── Ingredients ─────────────────────── │
│  Ingredient      Qty   Unit  Waste%  Cost│
│  Paneer          200   g     5%     ₹67  │
│  Butter           30   g     0%      ₹8  │
│  Tomato          150   g     10%    ₹10  │
│  Cream            50   ml    0%     ₹12  │
│  ... (scrollable)                        │
│                                          │
│  + Add Ingredient                        │
│                                          │
│  ── Summary ─────────────────────────── │
│  Total Cost:    ₹82.50 per portion       │
│  Selling Price: ₹280.00                  │
│  Food Cost %:   29.5%  ← ≤30% ✅ Good    │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Save Recipe      │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

**Food cost % guidance:**
- ≤ 30% → Green "Good"
- 31–35% → Orange "Acceptable"
- \> 35% → Red "High — review pricing"

---

## 11. Inventory Ledger

The inventory ledger is append-only. Current stock = SUM(quantity_in) - SUM(quantity_out). The backend writes to it automatically on orders, GRN, transfers, etc. Frontend is mostly read-only with option for manual adjustments.

### 11.1 Stock Summary (Ledger-Based)

**API:** `GET /erp/inventory-ledger/summary`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `branch_id` | `string?` | user's branch | |
| `low_only` | `bool` | false | stock < reorder_point |

**Response:** (derived from `v_ingredient_stock_ledger` view)
```json
[
  {
    "ingredient_id": "ing-001",
    "ingredient_name": "Paneer",
    "unit": "g",
    "current_stock": 4500,
    "weighted_avg_cost": 0.32,
    "stock_value": 1440.00,
    "last_movement_at": "2026-04-11T12:00:00Z",
    "reorder_point": 2000,
    "is_low": false
  }
]
```

**Flutter widget:** Stock dashboard.
```
┌── Inventory Ledger ──────────────────────┐
│  🔍 Search ingredient...                 │
│  [All] [Low Stock ⚠️]   Branch: [Main ▼] │
├──────────────────────────────────────────┤
│  Paneer              4,500 g     ₹1,440  │
│  ████████████░░░░    Avg ₹0.32/g         │  ← progress bar of stock level
│                                          │
│  Tomato              1,200 g       ₹180  │
│  ████░░░░░░░░░░░░  ⚠️ Below reorder      │  ← orange if low
│                                          │
│  Butter                800 g       ₹360  │
│  ██████████░░░░░░    Avg ₹0.45/g         │
├──────────────────────────────────────────┤
│  Total Stock Value: ₹28,450              │
└──────────────────────────────────────────┘
```

- Tap ingredient → ledger transaction history (see 11.2)
- Low stock items highlighted in orange with ⚠️ badge

### 11.2 Ingredient Transaction History

**API:** `GET /erp/inventory-ledger/history`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `ingredient_id` | `string` | required | |
| `branch_id` | `string?` | user's branch | |
| `transaction_type` | `string?` | all | `purchase`/`consumption`/`adjustment_in`/`adjustment_out`/`wastage`/`transfer_in`/`transfer_out` |
| `limit` | `int` | 50 | max 200 |
| `offset` | `int` | 0 | |

**Response:**
```json
[
  {
    "id": "uuid",
    "transaction_type": "consumption",
    "quantity_in": 0,
    "quantity_out": 200,
    "unit_cost": 0.32,
    "reference_type": "order",
    "reference_id": "ORD-20260411-001",
    "notes": null,
    "created_by": "system",
    "created_at": "2026-04-11T13:00:00Z"
  },
  {
    "id": "uuid",
    "transaction_type": "purchase",
    "quantity_in": 5000,
    "quantity_out": 0,
    "unit_cost": 0.30,
    "reference_type": "grn",
    "reference_id": "GRN-1001",
    "notes": null,
    "created_by": "user-uuid",
    "created_at": "2026-04-10T10:00:00Z"
  }
]
```

**Flutter widget:** Timeline list.
```
┌── Paneer — Transaction History ──────────┐
│  Current Stock: 4,500 g                  │
│  [All] [Purchase] [Consumption] [Adjust] │
├──────────────────────────────────────────┤
│  ▼ −200 g   consumption                 │
│    Order ORD-20260411-001 · 11 Apr 1pm   │
├──────────────────────────────────────────┤
│  ▲ +5,000 g   purchase                  │
│    GRN GRN-1001 · 10 Apr 10am           │
├──────────────────────────────────────────┤
│  ▼ −150 g   wastage                     │
│    Manual · 9 Apr 6pm                    │
└──────────────────────────────────────────┘
```

- Green ▲ for inbound (`quantity_in > 0`)
- Red ▼ for outbound (`quantity_out > 0`)
- Tap reference → navigate to order/GRN detail

### 11.3 Manual Stock Adjustment

**API:** `POST /erp/inventory-ledger/adjust`

**Request body:**
```json
{
  "ingredient_id": "ing-001",
  "branch_id": "branch-uuid",
  "transaction_type": "adjustment_in",
  "quantity": 500,
  "unit_cost": 0.30,
  "notes": "Physical count correction"
}
```

Use `adjustment_in` to add stock, `adjustment_out` to remove, `wastage` for spoilage.

**Flutter widget:** Bottom sheet.
```
┌── Stock Adjustment ──────────────────────┐
│                                          │
│  Ingredient:  Paneer    Current: 4,500 g │
│                                          │
│  Type:   ○ Add Stock  ○ Remove  ○ Waste  │
│                                          │
│  Quantity *     [500       ] g           │
│  Cost/Unit      [0.30      ] ₹           │
│  Notes          [Physical count correct] │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Submit           │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

---

## 12. Vendor Management

### 12.1 List Vendors

**API:** `GET /erp/vendors`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `is_active` | `bool` | true | |
| `search` | `string?` | null | search name, phone, gst_number |
| `limit` | `int` | 50 | |
| `offset` | `int` | 0 | |

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "Fresh Farms Pvt Ltd",
    "contact_person": "Rajesh Kumar",
    "phone": "9876543210",
    "email": "rajesh@freshfarms.in",
    "gst_number": "07AABCF1234L1ZP",
    "payment_terms": 30,
    "credit_limit": 50000.00,
    "balance_due": 12500.00,
    "total_purchased": 85000.00,
    "total_paid": 72500.00,
    "is_active": true
  }
]
```

**Flutter widget:**
```
┌── Vendors ───────────────────────────────┐
│  🔍 Search vendor...                     │
├──────────────────────────────────────────┤
│  Fresh Farms Pvt Ltd                     │
│  Rajesh Kumar · 9876543210               │
│  Due: ₹12,500 / ₹50,000 limit           │  ← red if > 80% of credit limit
│  GSTIN: 07AABCF1234L1ZP                 │
├──────────────────────────────────────────┤
│  Sabzi Mandi Wholesale                   │
│  Amit Singh · 9123456789                 │
│  Due: ₹0 · Paid up ✅                    │
└──────────────────────────────────────────┘
│  (+) Add Vendor — FAB bottom right       │
```

### 12.2 Create/Update Vendor

**API:** `POST /erp/vendors` (create) / `PATCH /erp/vendors/{vendor_id}` (update)

**Request body:**
```json
{
  "name": "Fresh Farms Pvt Ltd",
  "contact_person": "Rajesh Kumar",
  "phone": "9876543210",
  "email": "rajesh@freshfarms.in",
  "address": "Plot 42, APMC Market, Azadpur",
  "city": "New Delhi",
  "state": "Delhi",
  "pincode": "110033",
  "gst_number": "07AABCF1234L1ZP",
  "pan_number": "AABCF1234L",
  "bank_name": "HDFC Bank",
  "bank_account_number": "50100123456789",
  "bank_ifsc": "HDFC0001234",
  "payment_terms": 30,
  "credit_limit": 50000.00,
  "notes": "Preferred vegetable supplier"
}
```

**Flutter widget:** Multi-section form.
```
┌── Add Vendor ────────────────────────────┐
│                                          │
│  ── Basic Info ──────────────────────    │
│  Name *           [Fresh Farms Pvt Ltd ] │
│  Contact Person   [Rajesh Kumar        ] │
│  Phone            [9876543210          ] │
│  Email            [rajesh@freshfarms.in] │
│                                          │
│  ── Address ─────────────────────────    │
│  Address          [Plot 42, APMC...    ] │
│  City             [New Delhi           ] │
│  State            [Delhi          ▼    ] │
│  Pincode          [110033              ] │
│                                          │
│  ── Tax Info ────────────────────────    │
│  GSTIN            [07AABCF1234L1ZP     ] │
│  PAN              [AABCF1234L          ] │
│                                          │
│  ── Bank Details ────────────────────    │
│  Bank             [HDFC Bank           ] │
│  Account No       [50100123456789      ] │
│  IFSC             [HDFC0001234         ] │
│                                          │
│  ── Terms ───────────────────────────    │
│  Payment Terms    [30] days              │
│  Credit Limit     ₹ [50,000]            │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Save Vendor      │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

**GSTIN validation (client-side):** 15 chars, format: `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$`

### 12.3 Vendor Detail

**API:** `GET /erp/vendors/{vendor_id}`

Shows vendor info + recent GRNs + payment history + balance.

**Flutter widget:** Tabbed detail.
```
┌── Fresh Farms Pvt Ltd ───────────────────┐
│                                          │
│  Due: ₹12,500      Total: ₹85,000       │
│  ████████████████░░░░  85% paid          │
│                                          │
│  [GRNs] [Payments] [POs] [Info]          │
│  ─────────────────────────────────────── │
│  GRN-1005  10 Apr   ₹8,500   verified   │
│  GRN-1003   5 Apr   ₹4,000   verified   │
│  GRN-1001   1 Apr  ₹12,000   verified   │
│                                          │
│  ┌──────────────────────────────┐        │
│  │  Record Payment              │        │
│  └──────────────────────────────┘        │
└──────────────────────────────────────────┘
```

---

## 13. Goods Receipt Notes (GRN)

GRN records what was physically received against a purchase order. Verifying a GRN triggers inventory ledger writes and a journal entry (DR Inventory, CR Accounts Payable).

### 13.1 List GRNs

**API:** `GET /erp/grn`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `status` | `draft`/`verified`/`cancelled` | all | |
| `vendor_id` | `string?` | null | |
| `start_date` | `YYYY-MM-DD` | null | |
| `end_date` | `YYYY-MM-DD` | null | |
| `limit` | `int` | 50 | |
| `offset` | `int` | 0 | |

**Response:**
```json
[
  {
    "id": "uuid",
    "grn_number": "GRN-1005",
    "vendor_name": "Fresh Farms Pvt Ltd",
    "purchase_order_id": 42,
    "received_date": "2026-04-10",
    "total_amount": 8500.00,
    "status": "draft",
    "items_count": 5,
    "received_by": "Amit",
    "created_at": "2026-04-10T10:30:00Z"
  }
]
```

### 13.2 Create GRN

**API:** `POST /erp/grn`

**Request body:**
```json
{
  "purchase_order_id": 42,
  "vendor_id": "vendor-uuid",
  "branch_id": "branch-uuid",
  "received_date": "2026-04-10",
  "notes": "All items received in good condition",
  "items": [
    {
      "ingredient_id": "ing-001",
      "ordered_quantity": 5000,
      "received_quantity": 4800,
      "rejected_quantity": 200,
      "unit": "g",
      "unit_cost": 0.32,
      "batch_number": "BATCH-0410",
      "expiry_date": "2026-04-20",
      "notes": "200g spoiled in transit"
    }
  ]
}
```

**Flutter widget:** GRN creation form.
```
┌── Create GRN ────────────────────────────┐
│                                          │
│  PO: #42 (Fresh Farms)   [Change ▼]     │
│  Date: [10 Apr 2026]                     │
│                                          │
│  ── Items ───────────────────────────── │
│  Ingredient   Ordered  Received Rejected │
│  Paneer       5,000 g  [4,800]  [200  ] │
│   Cost: ₹0.32/g  Batch: [BATCH-0410]    │
│   Expiry: [20 Apr 2026]                 │
│  ─────────────────────────────────────── │
│  Tomato      10,000 g  [10,000] [0    ] │
│   Cost: ₹0.015/g  Batch: [________]     │
│  ─────────────────────────────────────── │
│                                          │
│  Total: ₹8,536.00                        │
│                                          │
│  ┌─────────────┐  ┌─────────────────┐    │
│  │ Save Draft   │  │ Save & Verify  │    │
│  └─────────────┘  └─────────────────┘    │
└──────────────────────────────────────────┘
```

- "Save Draft" → `status: 'draft'`
- "Save & Verify" → `status: 'verified'` (triggers event)
- Pre-fill items from PO if `purchase_order_id` provided

### 13.3 Verify GRN

**API:** `PATCH /erp/grn/{grn_id}/verify`

Transitions from `draft` → `verified`. Triggers:
1. Inventory ledger entries (purchase-in per item)
2. Journal: DR Inventory (1004), CR Accounts Payable (2001)

Show confirmation: "Verifying GRN will update inventory and accounting. Proceed?"

---

## 14. Vendor Payments

### 14.1 List Vendor Payments

**API:** `GET /erp/vendor-payments`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `vendor_id` | `string?` | null | filter |
| `payment_method` | `cash`/`bank_transfer`/`cheque`/`upi`/`other` | all | |
| `start_date` | `YYYY-MM-DD` | null | |
| `end_date` | `YYYY-MM-DD` | null | |
| `limit` | `int` | 50 | |
| `offset` | `int` | 0 | |

**Response:**
```json
[
  {
    "id": "uuid",
    "vendor_name": "Fresh Farms Pvt Ltd",
    "amount": 15000.00,
    "payment_method": "bank_transfer",
    "payment_date": "2026-04-10",
    "reference_number": "UTR-123456789",
    "grn_number": "GRN-1005",
    "notes": "April settlement",
    "created_by": "owner-uuid"
  }
]
```

### 14.2 Record Vendor Payment

**API:** `POST /erp/vendor-payments`

**Request body:**
```json
{
  "vendor_id": "vendor-uuid",
  "amount": 15000.00,
  "payment_method": "bank_transfer",
  "payment_date": "2026-04-10",
  "reference_number": "UTR-123456789",
  "purchase_order_id": 42,
  "grn_id": "grn-uuid",
  "notes": "April settlement"
}
```

Triggers journal: DR Accounts Payable (2001), CR Cash/Bank (1001/1002).

**Flutter widget:** Payment form.
```
┌── Record Vendor Payment ─────────────────┐
│                                          │
│  Vendor:  [Fresh Farms Pvt Ltd    ▼]    │
│  Balance Due: ₹12,500                    │
│                                          │
│  Amount *        ₹ [15,000     ]         │
│  Method *        [Bank Transfer   ▼]    │
│  Date            [10 Apr 2026     ]      │
│  Reference No    [UTR-123456789   ]      │
│  Link to GRN     [GRN-1005       ▼]    │
│  Notes           [April settlement ]     │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Record Payment   │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

---

## 15. Cash Drawer & Shift Management

Cash shifts track every cash transaction during a shift. At close, the cashier counts physical cash and the system compares it to expected amount.

### 15.1 Open Shift

**API:** `POST /erp/shifts/open`

**Request body:**
```json
{
  "drawer_id": "drawer-uuid",
  "opening_cash": 5000.00
}
```

**Response:**
```json
{
  "id": "shift-uuid",
  "drawer_name": "Main Counter",
  "user_id": "cashier-uuid",
  "opened_at": "2026-04-11T09:00:00Z",
  "opening_cash": 5000.00,
  "status": "open"
}
```

### 15.2 Current Shift Status

**API:** `GET /erp/shifts/current`

**Response:**
```json
{
  "id": "shift-uuid",
  "drawer_name": "Main Counter",
  "opened_at": "2026-04-11T09:00:00Z",
  "opening_cash": 5000.00,
  "status": "open",
  "summary": {
    "total_sales_cash": 12500.00,
    "total_sales_digital": 8300.00,
    "total_refunds": 850.00,
    "total_expenses": 1200.00,
    "total_cash_in": 500.00,
    "total_cash_out": 300.00,
    "expected_cash": 15650.00
  },
  "recent_transactions": [
    {
      "type": "sale",
      "amount": 850.00,
      "payment_method": "cash",
      "reference_id": "ORD-20260411-015",
      "created_at": "2026-04-11T14:30:00Z"
    }
  ]
}
```

**Flutter widget:** Shift dashboard (shown to cashier).
```
┌── Shift: Main Counter ───────────────────┐
│  Opened: 9:00 AM · Running 5h 30m       │
│                                          │
│  ┌─────────────┐  ┌─────────────┐       │
│  │  Expected    │  │  Opening    │       │
│  │  ₹15,650    │  │  ₹5,000     │       │
│  └─────────────┘  └─────────────┘       │
│                                          │
│  ── Breakdown ───────────────────────── │
│  Cash Sales        +₹12,500             │
│  Digital Sales     +₹8,300              │
│  Refunds           −₹850                │
│  Expenses          −₹1,200              │
│  Cash In           +₹500               │
│  Cash Out          −₹300               │
│                                          │
│  ── Recent Transactions ─────────────── │
│  ▲ ₹850   sale  ORD-015   2:30 PM      │
│  ▼ ₹200   expense         2:15 PM      │
│  ▲ ₹1,200 sale  ORD-014   2:00 PM      │
│                                          │
│  ┌─────────────────────────────────┐     │
│  │  Close Shift                    │     │
│  └─────────────────────────────────┘     │
└──────────────────────────────────────────┘
```

### 15.3 Close Shift

**API:** `POST /erp/shifts/{shift_id}/close`

**Request body:**
```json
{
  "closing_cash": 15800.00,
  "notes": "₹150 difference — found coins in tip jar"
}
```

**Response:**
```json
{
  "id": "shift-uuid",
  "status": "closed",
  "opening_cash": 5000.00,
  "closing_cash": 15800.00,
  "expected_cash": 15650.00,
  "cash_difference": 150.00,
  "notes": "₹150 difference — found coins in tip jar"
}
```

**Closing flow:**
```
┌── Close Shift ───────────────────────────┐
│                                          │
│  Expected Cash:  ₹15,650.00             │
│                                          │
│  Count Cash *    ₹ [15,800     ]         │
│                                          │
│  Difference:     +₹150.00  (surplus)    │  ← green if positive, red if negative
│                                          │
│  Notes           [Found coins in tip..] │
│                                          │
│  ┌────────┐  ┌────────────────────┐      │
│  │ Cancel │  │   Close Shift      │      │
│  └────────┘  └────────────────────┘      │
└──────────────────────────────────────────┘
```

**Difference color coding:**
- `0` → Green ✅ "Exact match"
- `> 0` → Blue "₹X surplus"
- `< 0` → Red ⚠️ "₹X shortage — investigate"

### 15.4 Shift History

**API:** `GET /erp/shifts`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `status` | `open`/`closed`/`reconciled` | all | |
| `user_id` | `string?` | null | filter by cashier |
| `start_date` | `YYYY-MM-DD` | null | |
| `end_date` | `YYYY-MM-DD` | null | |
| `limit` | `int` | 50 | |
| `offset` | `int` | 0 | |

### 15.5 Cash Drawers (Setup)

**API:** `GET /erp/drawers` / `POST /erp/drawers`

Create and manage physical cash drawers (linked to branch).

```json
{
  "name": "Main Counter",
  "branch_id": "branch-uuid"
}
```

---

## 16. Inter-Branch Stock Transfers

### 16.1 List Transfers

**API:** `GET /erp/transfers`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `status` | `draft`/`approved`/`in_transit`/`received`/`cancelled` | all | |
| `direction` | `outgoing`/`incoming` | all | relative to current branch |
| `limit` | `int` | 50 | |
| `offset` | `int` | 0 | |

**Response:**
```json
[
  {
    "id": "uuid",
    "transfer_number": "TRF-1001",
    "from_branch": "Main Kitchen",
    "to_branch": "Downtown Outlet",
    "status": "in_transit",
    "items_count": 3,
    "shipped_at": "2026-04-10T14:00:00Z",
    "requested_by": "Manager A"
  }
]
```

### 16.2 Create Transfer Request

**API:** `POST /erp/transfers`

**Request body:**
```json
{
  "from_branch_id": "main-branch-uuid",
  "to_branch_id": "downtown-branch-uuid",
  "notes": "Weekly supply to Downtown outlet",
  "items": [
    { "ingredient_id": "ing-001", "quantity_sent": 2000, "unit": "g" },
    { "ingredient_id": "ing-002", "quantity_sent": 500, "unit": "g" }
  ]
}
```

### 16.3 Transfer State Machine

```
draft → approved → in_transit → received
  ↓                                ↓
  cancelled                     cancelled (partial)
```

**APIs:**
- `PATCH /erp/transfers/{id}/approve` — manager approves
- `PATCH /erp/transfers/{id}/ship` — marks as in_transit, creates `transfer_out` ledger entries
- `PATCH /erp/transfers/{id}/receive` — records received quantities, creates `transfer_in` ledger entries

**Receive body** (may differ from sent):
```json
{
  "items": [
    { "ingredient_id": "ing-001", "quantity_received": 1950, "notes": "50g spillage" },
    { "ingredient_id": "ing-002", "quantity_received": 500 }
  ]
}
```

**Flutter widget:** Transfer detail.
```
┌── TRF-1001 ──────────────────────────────┐
│  Main Kitchen → Downtown Outlet          │
│  Status: 🚚 In Transit                   │
│  Shipped: 10 Apr 2:00 PM                │
│                                          │
│  ── Items ───────────────────────────── │
│  Ingredient     Sent     Received        │
│  Paneer         2,000 g  [1,950 ] g     │  ← editable when receiving
│  Butter           500 g  [  500 ] g     │
│                                          │
│  ┌─────────────────────────────────┐     │
│  │  Confirm Receipt                │     │  ← only at destination branch
│  └─────────────────────────────────┘     │
└──────────────────────────────────────────┘
```

---

## 17. GST Tax Configuration

### 17.1 List Tax Rates

**API:** `GET /erp/tax-rates`

Default rates seeded by `fn_seed_default_tax_rates()`. Owner can add custom rates.

**Response:**
```json
[
  {
    "id": "uuid",
    "name": "GST 5% (Restaurant)",
    "hsn_code": null,
    "rate_percentage": 5.00,
    "cgst_percentage": 2.50,
    "sgst_percentage": 2.50,
    "igst_percentage": 0,
    "is_inclusive": false,
    "applicable_on": "food",
    "is_exempt": false,
    "is_composition": false,
    "is_active": true
  }
]
```

**Flutter widget:**
```
┌── GST Tax Rates ─────────────────────────┐
│                                          │
│  GST 5% (Restaurant)           food      │
│  CGST 2.5% + SGST 2.5%     Exclusive    │
│                                          │
│  GST 5% (Restaurant) Incl      food      │
│  CGST 2.5% + SGST 2.5%     Inclusive    │
│                                          │
│  GST 18%                     service     │
│  CGST 9% + SGST 9%         Exclusive    │
│                                          │
│  IGST 5%                        food     │
│  Inter-state                 Exclusive    │
│                                          │
│  No GST (Alcohol)            Exempt 🔒   │
│                                          │
│  (+) Add Custom Tax Rate                 │
└──────────────────────────────────────────┘
```

### 17.2 Assign Tax to Items

**API:** `POST /erp/tax-rates/assign`

**Request body:**
```json
{
  "item_id": 42,
  "tax_rate_id": "tax-uuid"
}
```

**API:** `DELETE /erp/tax-rates/assign/{item_id}/{tax_rate_id}`

Add a tax rate dropdown on the **Item Edit** screen.

```
┌── Edit Item: Paneer Butter Masala ───────┐
│  ...                                     │
│  Tax Rate:  [GST 5% (Restaurant)    ▼]  │  ← dropdown of tax_rates
│  HSN Code:  [9963                    ]   │
│  ...                                     │
└──────────────────────────────────────────┘
```

---

## 18. GST Reports

### 18.1 Generate GST Report

**API:** `POST /erp/gst-reports/generate`

**Request body:**
```json
{
  "report_type": "GSTR1",
  "period_start": "2026-04-01",
  "period_end": "2026-04-30"
}
```

**Response:**
```json
{
  "id": "uuid",
  "report_type": "GSTR1",
  "period_start": "2026-04-01",
  "period_end": "2026-04-30",
  "total_sales": 450000.00,
  "total_taxable": 428571.43,
  "cgst_total": 10714.29,
  "sgst_total": 10714.29,
  "igst_total": 0,
  "total_tax": 21428.58,
  "b2b_count": 12,
  "b2c_count": 843,
  "status": "generated",
  "report_data": {
    "b2b": [...],
    "b2c_large": [...],
    "b2c_small": [...],
    "hsn_summary": [...]
  }
}
```

### 18.2 List GST Reports

**API:** `GET /erp/gst-reports`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `report_type` | `GSTR1`/`GSTR3B`/`tax_liability` | all | |
| `status` | `draft`/`generated`/`filed` | all | |

### 18.3 Mark as Filed

**API:** `PATCH /erp/gst-reports/{id}/filed`

Owner marks report as filed after uploading to GST portal.

**Flutter widget:** Monthly GST dashboard.
```
┌── GST Reports ───────────────────────────┐
│                                          │
│  Period: [April 2026  ▼]                │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │  Total Sales      ₹4,50,000       │  │
│  │  Total Taxable    ₹4,28,571       │  │
│  │  CGST Collected   ₹10,714         │  │
│  │  SGST Collected   ₹10,714         │  │
│  │  IGST Collected    ₹0             │  │
│  │  ─────────────────────────────    │  │
│  │  Total Tax        ₹21,429         │  │
│  │  B2B: 12  |  B2C: 843            │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌──────────────────┐                    │
│  │  GSTR-1   Generated ✅  [View]       │
│  │  GSTR-3B  Generated ✅  [View]       │
│  └──────────────────┘                    │
│                                          │
│  ┌──────────────────────────────┐        │
│  │  Generate Report              │        │
│  └──────────────────────────────┘        │
│                                          │
│  ┌──────────────────────────────┐        │
│  │  Mark as Filed on GST Portal  │        │
│  └──────────────────────────────┘        │
└──────────────────────────────────────────┘
```

### 18.4 Tax Liability View

**API:** `GET /erp/gst-reports/tax-liability`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `start_date` | `YYYY-MM-DD` | first of month | |
| `end_date` | `YYYY-MM-DD` | today | |

Returns month-wise breakdown for filing reminders.

---

## 19. Item Profitability

Populated automatically by the analytics aggregation job. Shows revenue vs COGS per menu item.

### 19.1 Profitability Report

**API:** `GET /erp/profitability`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `branch_id` | `string?` | user's branch | |
| `period_start` | `YYYY-MM-DD` | first of month | |
| `period_end` | `YYYY-MM-DD` | today | |
| `sort_by` | `margin_percent`/`gross_profit`/`quantity_sold`/`total_revenue` | `gross_profit` DESC | |
| `limit` | `int` | 50 | |

**Response:**
```json
[
  {
    "item_id": 42,
    "item_name": "Paneer Butter Masala",
    "quantity_sold": 320,
    "total_revenue": 89600.00,
    "total_cogs": 26400.00,
    "gross_profit": 63200.00,
    "margin_percent": 70.54
  },
  {
    "item_id": 15,
    "item_name": "Chicken Biryani",
    "quantity_sold": 280,
    "total_revenue": 84000.00,
    "total_cogs": 36400.00,
    "gross_profit": 47600.00,
    "margin_percent": 56.67
  }
]
```

**Flutter widget:** Profitability table + chart.
```
┌── Item Profitability (Apr 2026) ─────────┐
│  Branch: [Main ▼]   [1 Apr] → [11 Apr]  │
│                                          │
│  Item              Sold  Revenue   Margin│
│  ─────────────────────────────────────── │
│  Paneer Butter M.   320  ₹89,600  70.5% │  ← green
│  Chicken Biryani     280  ₹84,000  56.7% │  ← green
│  Dal Makhani         410  ₹49,200  62.3% │  ← green
│  Mutton Rogan Josh    95  ₹47,500  38.2% │  ← orange
│  Prawn Curry          45  ₹22,500  31.1% │  ← orange
│  Imported Truffle      8  ₹12,000  18.5% │  ← red ⚠️
│                                          │
│  ──── Chart (Top 10 by Profit) ──────── │
│  [Horizontal bar chart: revenue vs COGS] │
│  █████████████████░░░░░ Paneer BM        │
│  ████████████████░░░░░░ Chicken Biryani  │
│  ██████████░░░░░░░░░░░ Dal Makhani      │
└──────────────────────────────────────────┘
```

**Margin color coding:**
- \> 60% → Green "Excellent"
- 40–60% → Green "Good"
- 30–40% → Orange "Acceptable"
- < 30% → Red ⚠️ "Review pricing"

---

## 20. Daily P&L (Profit & Loss)

### 20.1 Daily P&L Report

**API:** `GET /erp/pnl/daily`

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `branch_id` | `string?` | user's branch | |
| `start_date` | `YYYY-MM-DD` | last 30 days | |
| `end_date` | `YYYY-MM-DD` | today | |

**Response:**
```json
[
  {
    "pnl_date": "2026-04-11",
    "total_revenue": 45000.00,
    "total_cogs": 13500.00,
    "gross_profit": 31500.00,
    "operating_expenses": 8000.00,
    "net_profit": 23500.00,
    "tax_collected": 2142.86,
    "total_orders": 85,
    "avg_order_value": 529.41
  }
]
```

**Flutter widget:** P&L with sparkline chart.
```
┌── Daily P&L ─────────────────────────────┐
│  Branch: [Main ▼]                        │
│  [1 Apr] → [11 Apr 2026]                │
│                                          │
│  ── Today (11 Apr) ─────────────────── │
│  Revenue:         ₹45,000                │
│  COGS:            −₹13,500               │
│  Gross Profit:    ₹31,500   (70.0%)     │
│  OpEx:            −₹8,000                │
│  ────────────────────────────           │
│  Net Profit:      ₹23,500   (52.2%)    │  ← big green number
│  Orders: 85  |  AOV: ₹529               │
│  Tax Collected: ₹2,143                   │
│                                          │
│  ── Trend (Last 11 Days) ───────────── │
│  [Line chart: Revenue, COGS, Net Profit] │
│   ╱‾‾‾‾╲    ╱‾‾‾‾╲                      │
│  ╱      ╲──╱      ╲──╱‾‾  ← Revenue     │
│  ──────────────────────── ← Net Profit   │
│  ════════════════════════ ← COGS         │
│                                          │
│  ── Summary (Period) ───────────────── │
│  Total Revenue:   ₹3,85,000             │
│  Total Net Profit: ₹1,98,500            │
│  Avg Daily Profit: ₹18,045              │
└──────────────────────────────────────────┘
```

Use `syncfusion_flutter_charts` or `fl_chart` for the trend lines.

---

## 21. Updated Navigation Structure

Add the new ERP modules to the admin drawer/sidebar:

```
📊 Dashboard
📋 Orders
🍽️ Menu
   └── Items (+ AI ingredient + Tax rate + Recipe per item)
📦 Inventory
   ├── Stock Levels       (existing)
   ├── Inventory Ledger   ← NEW (Section 11)
   └── Stock Transfers    ← NEW (Section 16)
👥 Vendors                ← NEW (Section 12)
   ├── Vendor List
   ├── GRN               ← NEW (Section 13)
   └── Payments           ← NEW (Section 14)
💰 Accounting
   ├── Cash Flow          (existing)
   ├── Entries            (existing)
   ├── Chart of Accounts  ← NEW (Section 8)
   ├── Journal Entries    ← NEW (Section 9)
   └── Record Expense     (existing)
💵 Cash Shifts            ← NEW (Section 15)
📊 Reports
   ├── Analytics          (existing)
   ├── Item Profitability ← NEW (Section 19)
   ├── Daily P&L          ← NEW (Section 20)
   └── GST Reports        ← NEW (Section 18)
🏷️ GST / Tax             ← NEW (Section 17)
   ├── Tax Rates
   └── Item Tax Mapping
🍳 Recipes                ← NEW (Section 10)
⚙️ Settings
```

---

## 22. New Dart Models

```dart
// chart_of_accounts.dart
class Account {
  final String id;
  final String accountCode;
  final String name;
  final String accountType; // asset, liability, equity, revenue, expense
  final String? parentId;
  final String? description;
  final bool isSystem;
  final bool isActive;

  Account.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      accountCode = json['account_code'],
      name = json['name'],
      accountType = json['account_type'],
      parentId = json['parent_id'],
      description = json['description'],
      isSystem = json['is_system'] ?? false,
      isActive = json['is_active'] ?? true;
}

// account_balance.dart
class AccountBalance {
  final String accountCode;
  final String name;
  final String accountType;
  final double totalDebit;
  final double totalCredit;
  final double balance;

  AccountBalance.fromJson(Map<String, dynamic> json)
    : accountCode = json['account_code'],
      name = json['name'],
      accountType = json['account_type'],
      totalDebit = (json['total_debit'] as num).toDouble(),
      totalCredit = (json['total_credit'] as num).toDouble(),
      balance = (json['balance'] as num).toDouble();
}

// journal_entry.dart
class JournalEntry {
  final String id;
  final String entryDate;
  final String referenceType;
  final String? referenceId;
  final String? description;
  final bool isReversed;
  final String createdBy;
  final DateTime createdAt;
  final List<JournalLine> lines;
  final double totalDebit;
  final double totalCredit;

  JournalEntry.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      entryDate = json['entry_date'],
      referenceType = json['reference_type'],
      referenceId = json['reference_id'],
      description = json['description'],
      isReversed = json['is_reversed'] ?? false,
      createdBy = json['created_by'],
      createdAt = DateTime.parse(json['created_at']),
      lines = (json['lines'] as List?)?.map((l) => JournalLine.fromJson(l)).toList() ?? [],
      totalDebit = (json['total_debit'] as num?)?.toDouble() ?? 0,
      totalCredit = (json['total_credit'] as num?)?.toDouble() ?? 0;
}

class JournalLine {
  final String accountCode;
  final String accountName;
  final double debit;
  final double credit;
  final String? description;

  JournalLine.fromJson(Map<String, dynamic> json)
    : accountCode = json['account_code'],
      accountName = json['account_name'],
      debit = (json['debit'] as num).toDouble(),
      credit = (json['credit'] as num).toDouble(),
      description = json['description'];
}

// recipe.dart
class Recipe {
  final String id;
  final int itemId;
  final String? itemName;
  final String? name;
  final double yieldQuantity;
  final String yieldUnit;
  final bool isActive;
  final double? totalCost;
  final List<RecipeIngredient> ingredients;

  Recipe.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      itemId = json['item_id'],
      itemName = json['item_name'],
      name = json['name'],
      yieldQuantity = (json['yield_quantity'] as num).toDouble(),
      yieldUnit = json['yield_unit'] ?? 'portion',
      isActive = json['is_active'] ?? true,
      totalCost = (json['total_cost'] as num?)?.toDouble(),
      ingredients = (json['ingredients'] as List?)?.map((i) => RecipeIngredient.fromJson(i)).toList() ?? [];
}

class RecipeIngredient {
  final String id;
  final String ingredientId;
  final String? ingredientName;
  final double quantityRequired;
  final String? unit;
  final double wastePercent;
  final double? unitCost;
  final double? lineCost;

  RecipeIngredient.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      ingredientId = json['ingredient_id'],
      ingredientName = json['ingredient_name'],
      quantityRequired = (json['quantity_required'] as num).toDouble(),
      unit = json['unit'],
      wastePercent = (json['waste_percent'] as num?)?.toDouble() ?? 0,
      unitCost = (json['unit_cost'] as num?)?.toDouble(),
      lineCost = (json['line_cost'] as num?)?.toDouble();
}

// inventory_ledger.dart
class StockSummary {
  final String ingredientId;
  final String ingredientName;
  final String unit;
  final double currentStock;
  final double weightedAvgCost;
  final double stockValue;
  final DateTime? lastMovementAt;
  final double? reorderPoint;
  final bool isLow;

  StockSummary.fromJson(Map<String, dynamic> json)
    : ingredientId = json['ingredient_id'].toString(),
      ingredientName = json['ingredient_name'],
      unit = json['unit'] ?? '',
      currentStock = (json['current_stock'] as num).toDouble(),
      weightedAvgCost = (json['weighted_avg_cost'] as num).toDouble(),
      stockValue = (json['stock_value'] as num?)?.toDouble() ?? 0,
      lastMovementAt = json['last_movement_at'] != null ? DateTime.parse(json['last_movement_at']) : null,
      reorderPoint = (json['reorder_point'] as num?)?.toDouble(),
      isLow = json['is_low'] ?? false;
}

class LedgerTransaction {
  final String id;
  final String transactionType;
  final double quantityIn;
  final double quantityOut;
  final double unitCost;
  final String? referenceType;
  final String? referenceId;
  final String? notes;
  final String? createdBy;
  final DateTime createdAt;

  LedgerTransaction.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      transactionType = json['transaction_type'],
      quantityIn = (json['quantity_in'] as num).toDouble(),
      quantityOut = (json['quantity_out'] as num).toDouble(),
      unitCost = (json['unit_cost'] as num?)?.toDouble() ?? 0,
      referenceType = json['reference_type'],
      referenceId = json['reference_id'],
      notes = json['notes'],
      createdBy = json['created_by'],
      createdAt = DateTime.parse(json['created_at']);
}

// vendor.dart
class Vendor {
  final String id;
  final String name;
  final String? contactPerson;
  final String? phone;
  final String? email;
  final String? gstNumber;
  final int paymentTerms;
  final double creditLimit;
  final double? balanceDue;
  final double? totalPurchased;
  final double? totalPaid;
  final bool isActive;

  Vendor.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      name = json['name'],
      contactPerson = json['contact_person'],
      phone = json['phone'],
      email = json['email'],
      gstNumber = json['gst_number'],
      paymentTerms = json['payment_terms'] ?? 30,
      creditLimit = (json['credit_limit'] as num?)?.toDouble() ?? 0,
      balanceDue = (json['balance_due'] as num?)?.toDouble(),
      totalPurchased = (json['total_purchased'] as num?)?.toDouble(),
      totalPaid = (json['total_paid'] as num?)?.toDouble(),
      isActive = json['is_active'] ?? true;
}

// grn.dart
class GoodsReceiptNote {
  final String id;
  final String grnNumber;
  final String? vendorName;
  final int? purchaseOrderId;
  final String receivedDate;
  final double totalAmount;
  final String status;
  final int? itemsCount;
  final List<GrnItem>? items;

  GoodsReceiptNote.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      grnNumber = json['grn_number'],
      vendorName = json['vendor_name'],
      purchaseOrderId = json['purchase_order_id'],
      receivedDate = json['received_date'],
      totalAmount = (json['total_amount'] as num).toDouble(),
      status = json['status'],
      itemsCount = json['items_count'],
      items = (json['items'] as List?)?.map((i) => GrnItem.fromJson(i)).toList();
}

class GrnItem {
  final String ingredientId;
  final String? ingredientName;
  final double orderedQuantity;
  final double receivedQuantity;
  final double rejectedQuantity;
  final String? unit;
  final double unitCost;
  final double lineTotal;
  final String? batchNumber;
  final String? expiryDate;

  GrnItem.fromJson(Map<String, dynamic> json)
    : ingredientId = json['ingredient_id'].toString(),
      ingredientName = json['ingredient_name'],
      orderedQuantity = (json['ordered_quantity'] as num?)?.toDouble() ?? 0,
      receivedQuantity = (json['received_quantity'] as num).toDouble(),
      rejectedQuantity = (json['rejected_quantity'] as num?)?.toDouble() ?? 0,
      unit = json['unit'],
      unitCost = (json['unit_cost'] as num?)?.toDouble() ?? 0,
      lineTotal = (json['line_total'] as num?)?.toDouble() ?? 0,
      batchNumber = json['batch_number'],
      expiryDate = json['expiry_date'];
}

// shift.dart
class Shift {
  final String id;
  final String? drawerName;
  final String userId;
  final DateTime openedAt;
  final DateTime? closedAt;
  final double openingCash;
  final double? closingCash;
  final double? expectedCash;
  final double? cashDifference;
  final String status;
  final ShiftSummary? summary;

  Shift.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      drawerName = json['drawer_name'],
      userId = json['user_id'],
      openedAt = DateTime.parse(json['opened_at']),
      closedAt = json['closed_at'] != null ? DateTime.parse(json['closed_at']) : null,
      openingCash = (json['opening_cash'] as num).toDouble(),
      closingCash = (json['closing_cash'] as num?)?.toDouble(),
      expectedCash = (json['expected_cash'] as num?)?.toDouble(),
      cashDifference = (json['cash_difference'] as num?)?.toDouble(),
      status = json['status'],
      summary = json['summary'] != null ? ShiftSummary.fromJson(json['summary']) : null;
}

class ShiftSummary {
  final double totalSalesCash;
  final double totalSalesDigital;
  final double totalRefunds;
  final double totalExpenses;
  final double totalCashIn;
  final double totalCashOut;
  final double expectedCash;

  ShiftSummary.fromJson(Map<String, dynamic> json)
    : totalSalesCash = (json['total_sales_cash'] as num).toDouble(),
      totalSalesDigital = (json['total_sales_digital'] as num).toDouble(),
      totalRefunds = (json['total_refunds'] as num).toDouble(),
      totalExpenses = (json['total_expenses'] as num).toDouble(),
      totalCashIn = (json['total_cash_in'] as num).toDouble(),
      totalCashOut = (json['total_cash_out'] as num).toDouble(),
      expectedCash = (json['expected_cash'] as num).toDouble();
}

// stock_transfer.dart
class StockTransfer {
  final String id;
  final String transferNumber;
  final String? fromBranch;
  final String? toBranch;
  final String status;
  final int? itemsCount;
  final DateTime? shippedAt;
  final DateTime? receivedAt;
  final List<TransferItem>? items;

  StockTransfer.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      transferNumber = json['transfer_number'],
      fromBranch = json['from_branch'],
      toBranch = json['to_branch'],
      status = json['status'],
      itemsCount = json['items_count'],
      shippedAt = json['shipped_at'] != null ? DateTime.parse(json['shipped_at']) : null,
      receivedAt = json['received_at'] != null ? DateTime.parse(json['received_at']) : null,
      items = (json['items'] as List?)?.map((i) => TransferItem.fromJson(i)).toList();
}

class TransferItem {
  final String ingredientId;
  final String? ingredientName;
  final double quantitySent;
  final double? quantityReceived;
  final String? unit;

  TransferItem.fromJson(Map<String, dynamic> json)
    : ingredientId = json['ingredient_id'].toString(),
      ingredientName = json['ingredient_name'],
      quantitySent = (json['quantity_sent'] as num).toDouble(),
      quantityReceived = (json['quantity_received'] as num?)?.toDouble(),
      unit = json['unit'];
}

// tax_rate.dart
class TaxRate {
  final String id;
  final String name;
  final String? hsnCode;
  final double ratePercentage;
  final double cgstPercentage;
  final double sgstPercentage;
  final double igstPercentage;
  final bool isInclusive;
  final String applicableOn;
  final bool isExempt;
  final bool isComposition;
  final bool isActive;

  TaxRate.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      name = json['name'],
      hsnCode = json['hsn_code'],
      ratePercentage = (json['rate_percentage'] as num).toDouble(),
      cgstPercentage = (json['cgst_percentage'] as num).toDouble(),
      sgstPercentage = (json['sgst_percentage'] as num).toDouble(),
      igstPercentage = (json['igst_percentage'] as num).toDouble(),
      isInclusive = json['is_inclusive'] ?? false,
      applicableOn = json['applicable_on'] ?? 'all',
      isExempt = json['is_exempt'] ?? false,
      isComposition = json['is_composition'] ?? false,
      isActive = json['is_active'] ?? true;
}

// gst_report.dart
class GstReport {
  final String id;
  final String reportType;
  final String periodStart;
  final String periodEnd;
  final double totalSales;
  final double totalTaxable;
  final double cgstTotal;
  final double sgstTotal;
  final double igstTotal;
  final double totalTax;
  final int b2bCount;
  final int b2cCount;
  final String status;
  final Map<String, dynamic>? reportData;

  GstReport.fromJson(Map<String, dynamic> json)
    : id = json['id'],
      reportType = json['report_type'],
      periodStart = json['period_start'],
      periodEnd = json['period_end'],
      totalSales = (json['total_sales'] as num).toDouble(),
      totalTaxable = (json['total_taxable'] as num).toDouble(),
      cgstTotal = (json['cgst_total'] as num).toDouble(),
      sgstTotal = (json['sgst_total'] as num).toDouble(),
      igstTotal = (json['igst_total'] as num).toDouble(),
      totalTax = (json['total_tax'] as num).toDouble(),
      b2bCount = json['b2b_count'] ?? 0,
      b2cCount = json['b2c_count'] ?? 0,
      status = json['status'],
      reportData = json['report_data'];
}

// item_profitability.dart
class ItemProfitability {
  final int itemId;
  final String itemName;
  final int quantitySold;
  final double totalRevenue;
  final double totalCogs;
  final double grossProfit;
  final double marginPercent;

  ItemProfitability.fromJson(Map<String, dynamic> json)
    : itemId = json['item_id'],
      itemName = json['item_name'],
      quantitySold = json['quantity_sold'] ?? 0,
      totalRevenue = (json['total_revenue'] as num).toDouble(),
      totalCogs = (json['total_cogs'] as num).toDouble(),
      grossProfit = (json['gross_profit'] as num).toDouble(),
      marginPercent = (json['margin_percent'] as num).toDouble();
}

// daily_pnl.dart
class DailyPnl {
  final String pnlDate;
  final double totalRevenue;
  final double totalCogs;
  final double grossProfit;
  final double operatingExpenses;
  final double netProfit;
  final double taxCollected;
  final int totalOrders;
  final double avgOrderValue;

  DailyPnl.fromJson(Map<String, dynamic> json)
    : pnlDate = json['pnl_date'],
      totalRevenue = (json['total_revenue'] as num).toDouble(),
      totalCogs = (json['total_cogs'] as num).toDouble(),
      grossProfit = (json['gross_profit'] as num).toDouble(),
      operatingExpenses = (json['operating_expenses'] as num).toDouble(),
      netProfit = (json['net_profit'] as num).toDouble(),
      taxCollected = (json['tax_collected'] as num).toDouble(),
      totalOrders = json['total_orders'] ?? 0,
      avgOrderValue = (json['avg_order_value'] as num).toDouble();
}
```

---

## 23. New API Services (Dart)

```dart
class ErpApi {
  final Dio _dio;
  ErpApi(this._dio);

  // ── Chart of Accounts ──
  Future<List<Account>> getAccounts({String? accountType}) async {
    final resp = await _dio.get('/erp/accounts', queryParameters: {
      if (accountType != null) 'account_type': accountType,
    });
    return (resp.data as List).map((e) => Account.fromJson(e)).toList();
  }

  Future<Account> createAccount(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/accounts', data: body);
    return Account.fromJson(resp.data);
  }

  Future<List<AccountBalance>> getTrialBalance({String? asOfDate}) async {
    final resp = await _dio.get('/erp/accounts/balances', queryParameters: {
      if (asOfDate != null) 'as_of_date': asOfDate,
    });
    return (resp.data as List).map((e) => AccountBalance.fromJson(e)).toList();
  }

  // ── Journal Entries ──
  Future<List<JournalEntry>> getJournals({
    String? referenceType, String? startDate, String? endDate,
    String? branchId, int limit = 50, int offset = 0,
  }) async {
    final resp = await _dio.get('/erp/journals', queryParameters: {
      if (referenceType != null) 'reference_type': referenceType,
      if (startDate != null) 'start_date': startDate,
      if (endDate != null) 'end_date': endDate,
      if (branchId != null) 'branch_id': branchId,
      'limit': limit, 'offset': offset,
    });
    return (resp.data as List).map((e) => JournalEntry.fromJson(e)).toList();
  }

  Future<JournalEntry> createJournal(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/journals', data: body);
    return JournalEntry.fromJson(resp.data);
  }

  Future<JournalEntry> reverseJournal(String journalId) async {
    final resp = await _dio.post('/erp/journals/$journalId/reverse');
    return JournalEntry.fromJson(resp.data);
  }

  // ── Recipes ──
  Future<List<Recipe>> getRecipes({int? itemId}) async {
    final resp = await _dio.get('/erp/recipes', queryParameters: {
      if (itemId != null) 'item_id': itemId,
    });
    return (resp.data as List).map((e) => Recipe.fromJson(e)).toList();
  }

  Future<Recipe> createRecipe(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/recipes', data: body);
    return Recipe.fromJson(resp.data);
  }

  Future<Recipe> updateRecipe(String recipeId, Map<String, dynamic> body) async {
    final resp = await _dio.patch('/erp/recipes/$recipeId', data: body);
    return Recipe.fromJson(resp.data);
  }

  // ── Inventory Ledger ──
  Future<List<StockSummary>> getStockSummary({String? branchId, bool lowOnly = false}) async {
    final resp = await _dio.get('/erp/inventory-ledger/summary', queryParameters: {
      if (branchId != null) 'branch_id': branchId,
      'low_only': lowOnly,
    });
    return (resp.data as List).map((e) => StockSummary.fromJson(e)).toList();
  }

  Future<List<LedgerTransaction>> getLedgerHistory({
    required String ingredientId, String? branchId, String? transactionType,
    int limit = 50, int offset = 0,
  }) async {
    final resp = await _dio.get('/erp/inventory-ledger/history', queryParameters: {
      'ingredient_id': ingredientId,
      if (branchId != null) 'branch_id': branchId,
      if (transactionType != null) 'transaction_type': transactionType,
      'limit': limit, 'offset': offset,
    });
    return (resp.data as List).map((e) => LedgerTransaction.fromJson(e)).toList();
  }

  Future<void> adjustStock(Map<String, dynamic> body) async {
    await _dio.post('/erp/inventory-ledger/adjust', data: body);
  }

  // ── Vendors ──
  Future<List<Vendor>> getVendors({String? search, int limit = 50, int offset = 0}) async {
    final resp = await _dio.get('/erp/vendors', queryParameters: {
      if (search != null) 'search': search,
      'limit': limit, 'offset': offset,
    });
    return (resp.data as List).map((e) => Vendor.fromJson(e)).toList();
  }

  Future<Vendor> createVendor(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/vendors', data: body);
    return Vendor.fromJson(resp.data);
  }

  Future<Vendor> updateVendor(String vendorId, Map<String, dynamic> body) async {
    final resp = await _dio.patch('/erp/vendors/$vendorId', data: body);
    return Vendor.fromJson(resp.data);
  }

  // ── GRN ──
  Future<List<GoodsReceiptNote>> getGrns({
    String? status, String? vendorId, int limit = 50, int offset = 0,
  }) async {
    final resp = await _dio.get('/erp/grn', queryParameters: {
      if (status != null) 'status': status,
      if (vendorId != null) 'vendor_id': vendorId,
      'limit': limit, 'offset': offset,
    });
    return (resp.data as List).map((e) => GoodsReceiptNote.fromJson(e)).toList();
  }

  Future<GoodsReceiptNote> createGrn(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/grn', data: body);
    return GoodsReceiptNote.fromJson(resp.data);
  }

  Future<GoodsReceiptNote> verifyGrn(String grnId) async {
    final resp = await _dio.patch('/erp/grn/$grnId/verify');
    return GoodsReceiptNote.fromJson(resp.data);
  }

  // ── Vendor Payments ──
  Future<void> recordVendorPayment(Map<String, dynamic> body) async {
    await _dio.post('/erp/vendor-payments', data: body);
  }

  // ── Shifts ──
  Future<Shift> openShift(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/shifts/open', data: body);
    return Shift.fromJson(resp.data);
  }

  Future<Shift?> getCurrentShift() async {
    final resp = await _dio.get('/erp/shifts/current');
    if (resp.data == null) return null;
    return Shift.fromJson(resp.data);
  }

  Future<Shift> closeShift(String shiftId, Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/shifts/$shiftId/close', data: body);
    return Shift.fromJson(resp.data);
  }

  // ── Transfers ──
  Future<List<StockTransfer>> getTransfers({String? status, int limit = 50, int offset = 0}) async {
    final resp = await _dio.get('/erp/transfers', queryParameters: {
      if (status != null) 'status': status,
      'limit': limit, 'offset': offset,
    });
    return (resp.data as List).map((e) => StockTransfer.fromJson(e)).toList();
  }

  Future<StockTransfer> createTransfer(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/transfers', data: body);
    return StockTransfer.fromJson(resp.data);
  }

  Future<void> approveTransfer(String id) async => await _dio.patch('/erp/transfers/$id/approve');
  Future<void> shipTransfer(String id) async => await _dio.patch('/erp/transfers/$id/ship');
  Future<void> receiveTransfer(String id, Map<String, dynamic> body) async {
    await _dio.patch('/erp/transfers/$id/receive', data: body);
  }

  // ── Tax Rates ──
  Future<List<TaxRate>> getTaxRates() async {
    final resp = await _dio.get('/erp/tax-rates');
    return (resp.data as List).map((e) => TaxRate.fromJson(e)).toList();
  }

  Future<void> assignTaxToItem(int itemId, String taxRateId) async {
    await _dio.post('/erp/tax-rates/assign', data: {
      'item_id': itemId, 'tax_rate_id': taxRateId,
    });
  }

  // ── GST Reports ──
  Future<GstReport> generateGstReport(Map<String, dynamic> body) async {
    final resp = await _dio.post('/erp/gst-reports/generate', data: body);
    return GstReport.fromJson(resp.data);
  }

  Future<List<GstReport>> getGstReports({String? reportType, String? status}) async {
    final resp = await _dio.get('/erp/gst-reports', queryParameters: {
      if (reportType != null) 'report_type': reportType,
      if (status != null) 'status': status,
    });
    return (resp.data as List).map((e) => GstReport.fromJson(e)).toList();
  }

  Future<void> markGstReportFiled(String reportId) async {
    await _dio.patch('/erp/gst-reports/$reportId/filed');
  }

  // ── Profitability ──
  Future<List<ItemProfitability>> getProfitability({
    String? branchId, String? periodStart, String? periodEnd,
    String sortBy = 'gross_profit', int limit = 50,
  }) async {
    final resp = await _dio.get('/erp/profitability', queryParameters: {
      if (branchId != null) 'branch_id': branchId,
      if (periodStart != null) 'period_start': periodStart,
      if (periodEnd != null) 'period_end': periodEnd,
      'sort_by': sortBy, 'limit': limit,
    });
    return (resp.data as List).map((e) => ItemProfitability.fromJson(e)).toList();
  }

  // ── Daily P&L ──
  Future<List<DailyPnl>> getDailyPnl({
    String? branchId, String? startDate, String? endDate,
  }) async {
    final resp = await _dio.get('/erp/pnl/daily', queryParameters: {
      if (branchId != null) 'branch_id': branchId,
      if (startDate != null) 'start_date': startDate,
      if (endDate != null) 'end_date': endDate,
    });
    return (resp.data as List).map((e) => DailyPnl.fromJson(e)).toList();
  }
}
```

---

## 24. Event-Driven Features (Automatic — No Frontend)

These now happen automatically via `erp_event_handlers.py`:

| Event | Backend Side-Effects | Where You See It |
|-------|---------------------|-------------------|
| Order confirmed | Ingredient deducted via ledger + COGS calculated + Journal: DR COGS, CR Inventory | Inventory Ledger, Journal Entries, Order detail (COGS field) |
| Order cancelled | Ledger reversed + COGS journal reversed | Inventory Ledger, Journal Entries |
| Payment completed | Revenue entry + Journal: DR Cash/Bank, CR Revenue + CR GST Payable | Accounting Entries, Journal Entries, Shift transactions |
| Payment refunded | Refund entry + Reverse journal | Accounting Entries, Journal Entries |
| GRN verified | Purchase-in ledger entries + Journal: DR Inventory, CR A/P | Inventory Ledger, Journal Entries, Vendor balances |

**No frontend changes needed for these** — just refresh data after order/payment flows.

---

## 25. Updated Checklist

### Phase 1 (Existing — already documented above)
- [ ] Accounting: cash flow, entries, daily breakdown, payment methods, record expense
- [ ] AI Ingredients: suggest + auto-link
- [ ] Navigation drawer updates

### Phase 2 (New ERP Modules)
- [ ] Run `migrations/006_erp_full_system.sql` in Supabase SQL Editor
- [ ] Chart of Accounts tree view (Section 8)
- [ ] Trial Balance table (Section 8.3)
- [ ] Journal Entries list with expandable lines (Section 9)
- [ ] Manual journal entry form with live balance check (Section 9.2)
- [ ] Journal reversal flow (Section 9.3)
- [ ] Recipe editor per menu item (Section 10)
- [ ] Food cost % indicator on recipe
- [ ] Inventory ledger stock summary (Section 11.1)
- [ ] Ingredient transaction history timeline (Section 11.2)
- [ ] Manual stock adjustment form (Section 11.3)
- [ ] Vendor list + create/edit form (Section 12)
- [ ] GSTIN validation regex on vendor form
- [ ] Vendor detail with GRN/payments tabs (Section 12.3)
- [ ] GRN creation from PO (Section 13.2)
- [ ] GRN verify flow with confirmation (Section 13.3)
- [ ] Vendor payment recording (Section 14.2)
- [ ] Cash drawer setup (Section 15.5)
- [ ] Shift open/close flow (Section 15.1–15.3)
- [ ] Shift dashboard for cashiers (Section 15.2)
- [ ] Shift history for managers (Section 15.4)
- [ ] Stock transfer creation (Section 16.2)
- [ ] Transfer state machine flow (approve → ship → receive) (Section 16.3)
- [ ] GST tax rates list (Section 17.1)
- [ ] Tax rate assignment on item edit screen (Section 17.2)
- [ ] GST report generation (Section 18.1)
- [ ] GST report dashboard with filing status (Section 18.3)
- [ ] Item profitability table + chart (Section 19)
- [ ] Daily P&L with trend chart (Section 20)
- [ ] Updated navigation structure (Section 21)
