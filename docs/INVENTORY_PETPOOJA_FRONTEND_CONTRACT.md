# Inventory — Petpooja-Parity Features: Frontend Contract

Scope: the 5 new capabilities added to the inventory module. This document is
self-contained — implement exactly these endpoints, request bodies, and
response shapes.

## Conventions

- **Base URL**: `/api/v1`
- **Auth**: `Authorization: Bearer <JWT>` on every request.
- **Tenancy**: restaurant/branch are derived server-side from the token — do
  NOT send `restaurant_id` / `branch_id` in bodies.
- **Money/qty**: JSON numbers. Quantities are decimals; money is 2-decimal.
- **Permissions** the logged-in user needs:
  - `inventory.read` — all GET inventory endpoints
  - `inventory.update` — create conversions, sales, returns
  - `purchase_order.read` / `purchase_order.write` — PO view / edit
  - `purchase_order.approve` — approve/reject POs (owner + manager only)
- **Errors**: `404` (not found), `400`/`422` (validation), `409`/`400` for
  business errors like insufficient stock. Error body: `{ "detail": "<msg>" }`.
- **Realtime (WebSocket)**: new stock events fan out on the inventory channel:
  `inventory.converted_in`, `inventory.converted_out`, `inventory.sold`
  (plus existing `inventory.transferred_out` / `inventory.transferred_in`).
  On receiving any of these, refresh affected ingredient balances.

---

## 1. Conversions — semi-finished goods (e.g. dosa batter)

A raw-material → semi-finished production step. Inputs are consumed, the output
ingredient's stock increases. The output must already exist as an ingredient
(mark it "semi-finished" when creating it).

### 1a. Conversion recipes (templates)

**GET** `/inventory/conversion-recipes?output_ingredient_id={id?}`
Perm: `inventory.read`. Returns an array:
```json
[
  {
    "id": "uuid",
    "output_ingredient_id": "ING123",
    "name": "Dosa Batter",
    "yield_quantity": 10.0,
    "yield_unit": "kg",
    "is_active": true,
    "notes": null,
    "created_at": "2026-07-02T10:00:00Z"
  }
]
```

**POST** `/inventory/conversion-recipes` → `201`
Perm: `inventory.update`. Body:
```json
{
  "output_ingredient_id": "ING123",
  "yield_quantity": 10.0,
  "yield_unit": "kg",
  "name": "Dosa Batter",
  "notes": "morning batch",
  "inputs": [
    { "ingredient_id": "ING_RICE", "quantity_required": 6, "unit": "kg", "waste_percent": 0 },
    { "ingredient_id": "ING_DAL",  "quantity_required": 2, "unit": "kg", "waste_percent": 0 }
  ]
}
```
`inputs` requires ≥ 1 item. Response = the recipe including an `inputs` array.

### 1b. Run a conversion (produce output)

**GET** `/inventory/conversions?limit=50&offset=0`
Perm: `inventory.read`. Array of past runs:
```json
[
  {
    "id": "uuid",
    "conversion_recipe_id": "uuid|null",
    "output_ingredient_id": "ING123",
    "produced_quantity": 10.0,
    "output_unit": "kg",
    "status": "completed",
    "created_at": "2026-07-02T10:05:00Z"
  }
]
```

**POST** `/inventory/conversions` → `201`
Perm: `inventory.update`. Provide EITHER `conversion_recipe_id` (inputs are
auto-scaled to `produced_quantity`) OR an explicit `inputs` list.
```json
{
  "output_ingredient_id": "ING123",
  "produced_quantity": 10.0,
  "output_unit": "kg",
  "conversion_recipe_id": "uuid",
  "inputs": null,
  "notes": "batch #1"
}
```
Response:
```json
{
  "conversion_id": "uuid",
  "output_ingredient_id": "ING123",
  "produced_quantity": 10.0,
  "inputs_consumed": [
    { "ingredient_id": "ING_RICE", "quantity": 6.0 },
    { "ingredient_id": "ING_DAL",  "quantity": 2.0 }
  ],
  "output_unit_cost": 12.5
}
```
Errors: `404` if output ingredient missing; `400` "insufficient stock for
input …" if any input lacks balance.

**UI**: a "Conversions" tab. List runs + a "Produce" form (pick recipe or output
ingredient, enter produced qty). Show consumed inputs and resulting balance.

---

## 2. Raw material sales

Sell raw stock to another outlet/party. Confirming a sale deducts stock.

**GET** `/inventory/sales?status={draft|confirmed|cancelled}&limit=50&offset=0`
Perm: `inventory.read`. Array of sale headers:
```json
[
  {
    "id": "uuid",
    "sale_number": "SAL-1001",
    "buyer_name": "Branch B",
    "buyer_gst": "27AAAAA0000A1Z5",
    "sub_total": 500.0,
    "tax_amount": 25.0,
    "total_amount": 525.0,
    "status": "confirmed",
    "created_at": "2026-07-02T11:00:00Z"
  }
]
```

**GET** `/inventory/sales/{sale_id}`
Perm: `inventory.read`. Header + line items:
```json
{
  "id": "uuid",
  "sale_number": "SAL-1001",
  "buyer_name": "Branch B",
  "total_amount": 525.0,
  "status": "confirmed",
  "items": [
    {
      "ingredient_id": "ING_RICE",
      "ingredient_name": "Rice",
      "quantity": 10.0,
      "unit": "kg",
      "unit_price": 50.0,
      "tax_percent": 5.0,
      "line_total": 525.0
    }
  ]
}
```

**POST** `/inventory/sales` → `201`
Perm: `inventory.update`. Body:
```json
{
  "items": [
    { "ingredient_id": "ING_RICE", "quantity": 10, "unit": "kg", "unit_price": 50, "tax_percent": 5 }
  ],
  "buyer_name": "Branch B",
  "buyer_gst": "27AAAAA0000A1Z5",
  "buyer_contact": "9999999999",
  "buyer_address": "…",
  "terms": "Net 15",
  "notes": null
}
```
Server computes `line_total`, `sub_total`, `tax_amount`, `total_amount` and
assigns `sale_number`. Response = full sale (same shape as GET by id).
Errors: `404` ingredient missing; `400` insufficient stock.

**UI**: a "Sales" tab: list + "New Sale" form (buyer details + line items with
qty/price/tax). Show computed totals live, submit, then display the invoice.

---

## 3. Outlet-to-outlet returns

Return previously-transferred stock back to the sender. A return is itself a
`stock_transfer` with `transfer_type = "return"`, moving from the original
destination branch back to the origin.

**POST** `/inventory/transfers/{transfer_id}/return`
Perm: `inventory.update`. Precondition: the original transfer's `status` must be
`received`. Body (omit `items` to return the full received quantities):
```json
{
  "items": [
    { "ingredient_id": "ING_RICE", "quantity": 4, "unit": "kg" }
  ],
  "notes": "excess returned"
}
```
Response:
```json
{
  "return_transfer_id": "uuid",
  "original_transfer_id": "uuid",
  "status": "draft",
  "transfer_type": "return"
}
```
Errors: `404` transfer not found; `400` "only a received transfer can be
returned", "a return transfer cannot itself be returned", or "cannot return
more than received".

**Then move the stock using the EXISTING transfer endpoints** on the returned
`return_transfer_id`:
- **POST** `/inventory/transfers/{return_transfer_id}/ship`
- **POST** `/inventory/transfers/{return_transfer_id}/receive`
  (body `{ "items": null }` to receive full sent qty, or per-line quantities).

**Note**: `GET /inventory/transfers` now includes `transfer_type` and
`original_transfer_id` on each row — show a "Return" badge when
`transfer_type == "return"`.

**UI**: on a received transfer's detail, add a "Return" action → choose full or
partial → creates the draft return → then Ship/Receive it like a normal transfer.

---

## 4. Purchase-order approval workflow + PDF

New field on every PO object: `approval_status` ∈
`draft | pending_approval | approved | rejected`, plus `requested_by`,
`approved_by`, `approved_at`, `rejected_reason`.

State machine:
```
draft ──submit──▶ pending_approval ──approve──▶ approved
  ▲                     │
  └───────rejected◀──reject──┘   (rejected can be submitted again)
```

**POST** `/purchase-orders/{po_id}/submit` — perm `purchase_order.write`
From `draft` or `rejected` → `pending_approval`. Returns the PO row.

**POST** `/purchase-orders/{po_id}/approve` — perm `purchase_order.approve`
From `pending_approval` → `approved` (sets `approved_by`, `approved_at`).

**POST** `/purchase-orders/{po_id}/reject` — perm `purchase_order.approve`
From `pending_approval` → `rejected`. Body:
```json
{ "reason": "prices too high" }
```

**GET** `/purchase-orders/{po_id}/pdf` — perm `purchase_order.read`
Returns `application/pdf` (`Content-Disposition: inline`). Open/download it for
printing or forwarding to the vendor.

Invalid transitions return `400` "cannot move approval from '<x>' to '<y>'".

**UI**: on the PO detail, show an approval-status chip. Buttons:
- Owner/staff with `purchase_order.write`: **Submit for approval** (when draft/rejected).
- Owner/manager (`purchase_order.approve`): **Approve** / **Reject** (with reason) when pending.
- **Download PDF** button for everyone with read.
> There is no email endpoint yet — use the PDF (print/share). Email can be added later.

---

## 5. Reports

**GET** `/inventory/reports/current-stock?low_only={true|false}`
Perm: `inventory.read`.
```json
{
  "items": [
    {
      "ingredient_id": "ING_RICE",
      "name": "Rice",
      "unit": "kg",
      "category": "Grains",
      "balance": 120.0,
      "cost_per_unit": 50.0,
      "valuation": 6000.0,
      "is_low_stock": false
    }
  ],
  "total_valuation": 6000.0
}
```

**GET** `/inventory/reports/variance?count_id={uuid?}&limit=200`
Perm: `inventory.read`. Variance from physical counts:
```json
{
  "items": [
    {
      "ingredient_id": "ING_RICE",
      "name": "Rice",
      "expected_qty": 120.0,
      "counted_qty": 118.0,
      "variance": -2.0,
      "unit": "kg",
      "unit_cost": 50.0,
      "variance_value": -100.0,
      "count_id": "uuid",
      "count_number": "CNT-12",
      "count_date": "2026-07-01",
      "status": "approved"
    }
  ]
}
```

**GET** `/inventory/reports/consumption-pnl?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
Perm: `inventory.read`. Consumption / cost rollup:
```json
{
  "items": [
    {
      "ingredient_id": "ING_RICE",
      "name": "Rice",
      "consumed_qty": 40.0,
      "purchased_qty": 100.0,
      "wasted_qty": 2.0,
      "cogs": 2000.0,
      "waste_value": 100.0
    }
  ],
  "totals": { "cogs": 2000.0, "waste_value": 100.0 }
}
```

**UI**: a "Reports" section with three tabs — Current Stock (with a low-stock
filter + total valuation), Variance (filter by count), and Consumption/P&L
(date range + totals).

---

## Permissions summary

| Capability                         | Permission required        |
|------------------------------------|----------------------------|
| View conversions/sales/reports     | `inventory.read`           |
| Create conversion / sale / return  | `inventory.update`         |
| Ship / receive transfers & returns | `inventory.update`         |
| View / edit / submit PO            | `purchase_order.read/write`|
| Approve / reject PO                | `purchase_order.approve`   |

Hide/disable actions the user lacks permission for.
