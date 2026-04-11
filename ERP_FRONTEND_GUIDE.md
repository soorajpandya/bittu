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
