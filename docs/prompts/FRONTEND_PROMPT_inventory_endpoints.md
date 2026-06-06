# Inventory Module — Backend API Contract

All routes are prefixed with `/api/v1`. All requests require the standard
`Authorization: Bearer <jwt>` header. Tenant scoping (`restaurant_id`,
`branch_id`, `user_id`) is derived from the token — never sent in the body.

Money / quantity fields accept either `number` or numeric string and always
come back as `number`. Dates are ISO-8601 UTC unless noted.

Permissions referenced:
- `inventory.read` / `inventory.update`
- `menu.read` / `menu.write` (for ingredient & item-ingredient master)
- `purchase_order.read` / `purchase_order.write`

---

## 1. Ingredient master — `/api/v1/ingredients`

The inventory master rows. Everything else (balances, ledger, adjustments,
wastage…) references `ingredient_id`.

### `GET /api/v1/ingredients?include_inactive=false`
Response `200`:
```json
[
  {
    "id": "ing_a1b2c3",
    "user_id": "usr_owner",
    "restaurant_id": "11111111-1111-1111-1111-111111111111",
    "branch_id": null,
    "name": "Tomato",
    "unit": "kg",
    "current_stock": 12.5,
    "stock_quantity": 12.5,
    "reorder_point": 5,
    "reorder_quantity": 20,
    "reorder_level": 5,
    "minimum_stock": 2,
    "cost_per_unit": 35.0,
    "category": "Produce",
    "storage_location": "Walk-in cooler A",
    "storage_type": "refrigerated",
    "is_perishable": true,
    "shelf_life_days": 7,
    "track_batches": true,
    "sku": "TOM-001",
    "barcode": "8901234567890",
    "supplier": "Green Farms",
    "is_active": true,
    "created_at": "2026-05-20T08:14:11Z",
    "updated_at": "2026-05-30T12:01:55Z",
    "deleted_at": null
  }
]
```

### `POST /api/v1/ingredients`  (perm: `menu.write`)
Idempotent on `(restaurant_id, lower(name))`. If `current_stock > 0`, an
`opening` ledger event is appended automatically.

Request:
```json
{
  "name": "Tomato",
  "unit": "kg",
  "current_stock": 12.5,
  "reorder_point": 5,
  "reorder_quantity": 20,
  "minimum_stock": 2,
  "cost_per_unit": 35.0,
  "category": "Produce",
  "storage_location": "Walk-in cooler A",
  "storage_type": "refrigerated",
  "is_perishable": true,
  "shelf_life_days": 7,
  "track_batches": true,
  "sku": "TOM-001",
  "barcode": "8901234567890",
  "supplier": "Green Farms",
  "branch_id": null
}
```
Response `201`:
```json
{
  "ingredient": { "...same shape as list row above..." },
  "created": true
}
```
On duplicate name `created` is `false` and the existing row is returned.

### `PATCH /api/v1/ingredients/{ingredient_id}`  (perm: `menu.write`)
Any subset of:
```json
{
  "name": "Roma Tomato",
  "unit": "kg",
  "reorder_point": 8,
  "reorder_quantity": 25,
  "minimum_stock": 3,
  "cost_per_unit": 38.0,
  "category": "Produce",
  "storage_location": "Walk-in cooler B",
  "storage_type": "refrigerated",
  "is_perishable": true,
  "shelf_life_days": 5,
  "track_batches": true,
  "sku": "TOM-002",
  "barcode": "8901234567891",
  "supplier": "Sunrise Farms",
  "is_active": true,
  "branch_id": null
}
```
**Stock quantity is NEVER changed by PATCH.** Use `/inventory/adjustments`,
`/inventory/wastage`, `/inventory/counts/.../finalize`, or a GRN.

Response: the updated row (same shape as list row).

### `DELETE /api/v1/ingredients/{ingredient_id}`  (perm: `menu.write`)
Soft delete. Response `200 { "ok": true }`.

---

## 2. Recipe (item → ingredient) — `/api/v1/item-ingredients`

Links a menu item to the ingredients it consumes. Drives auto-deduction on
order completion.

### `GET /api/v1/item-ingredients?item_id=123`
Response `200`:
```json
[
  {
    "id": 4501,
    "item_id": 123,
    "ingredient_id": "ing_a1b2c3",
    "ingredient_name": "Tomato",
    "quantity_used": 0.15,
    "unit": "kg",
    "created_at": "2026-05-25T10:11:00Z"
  }
]
```

### `POST /api/v1/item-ingredients`  (perm: `menu.write`)
Request:
```json
{
  "item_id": 123,
  "ingredient_id": "ing_a1b2c3",
  "quantity_used": 0.15,
  "unit": "kg"
}
```
Response `201`: the row above.

### `PATCH /api/v1/item-ingredients/{ii_id}`
```json
{ "quantity_used": 0.20, "unit": "kg" }
```

### `DELETE /api/v1/item-ingredients/{ii_id}`
`200 { "ok": true }`

---

## 3. AI ingredient assist — `/api/v1/ai-ingredients`

### `POST /api/v1/ai-ingredients/suggest`  (perm: `menu.write`)
```json
{ "item_name": "Paneer Butter Masala" }
```
Response `200` (does NOT save):
```json
{
  "item_name": "Paneer Butter Masala",
  "suggestions": [
    { "name": "Paneer",   "quantity": 0.2, "unit": "kg" },
    { "name": "Butter",   "quantity": 0.03, "unit": "kg" },
    { "name": "Tomato",   "quantity": 0.15, "unit": "kg" },
    { "name": "Cream",    "quantity": 0.05, "unit": "L"  }
  ]
}
```

### `POST /api/v1/ai-ingredients/auto-link`  (perm: `menu.write`)
Suggest → match/create master rows → link to the item in one call.
```json
{ "item_id": 123, "item_name": "Paneer Butter Masala" }
```
Response `201`:
```json
{
  "item_id": 123,
  "linked": [
    { "ingredient_id": "ing_xyz1", "name": "Paneer", "quantity_used": 0.2, "unit": "kg", "created_master": false },
    { "ingredient_id": "ing_xyz2", "name": "Butter", "quantity_used": 0.03, "unit": "kg", "created_master": true }
  ]
}
```

---

## 4. Live balances & ledger — `/api/v1/inventory`

Event-sourced. Balance is computed from `inventory_ledger` (not stored on
the master). Use these for any real-time stock display.

### `GET /api/v1/inventory/balances?branch_id={uuid?}`  (perm: `inventory.read`)
```json
[
  {
    "ingredient_id": "ing_a1b2c3",
    "name": "Tomato",
    "unit": "kg",
    "balance": 11.85,
    "branch_id": "22222222-2222-2222-2222-222222222222",
    "last_event_at": "2026-05-31T09:22:14Z",
    "reorder_point": 5,
    "is_low": false
  }
]
```

### `GET /api/v1/inventory/balance/{ingredient_id}?branch_id=&as_of=2026-05-30T23:59:59Z`
```json
{ "ingredient_id": "ing_a1b2c3", "balance": 11.85, "as_of": null }
```

### `GET /api/v1/inventory/timeline/{ingredient_id}?branch_id=&limit=100&offset=0`
```json
{
  "ingredient_id": "ing_a1b2c3",
  "total": 142,
  "events": [
    {
      "id": "evt_01HXYZ...",
      "occurred_at": "2026-05-31T09:22:14Z",
      "event_type": "INVENTORY_WASTED",
      "ledger_type": "wastage",
      "quantity_in": 0,
      "quantity_out": 0.65,
      "unit_cost": 35.0,
      "running_balance": 11.85,
      "reference_type": "wastage",
      "reference_id": "wst_5599",
      "source": "manual",
      "notes": "Bruised",
      "created_by": "usr_owner",
      "metadata": { "waste_reason": "spoilage" }
    }
  ]
}
```

### Legacy convenience
- `GET /api/v1/inventory/stock?branch_id=&low_only=false` — flat snapshot
  for older clients. Same row shape as `/balances` plus `cost_per_unit`.
- `POST /api/v1/inventory/receive` body `{ "purchase_order_id": "po_123" }`
  → posts INVENTORY_PURCHASED events for every PO line.

---

## 5. Snapshots — `/api/v1/inventory/snapshots`

### `POST /api/v1/inventory/snapshots/build?branch_id=&period=rolling`
`period`: `rolling | day | week | month`.
Response `200 { "built": 87, "period": "rolling" }`.

### `GET /api/v1/inventory/snapshots?ingredient_id=&branch_id=&period=rolling&limit=50`
```json
[
  {
    "id": "snap_01HX...",
    "restaurant_id": "...",
    "branch_id": "...",
    "ingredient_id": "ing_a1b2c3",
    "period": "rolling",
    "opening_qty": 12.5,
    "in_qty": 0,
    "out_qty": 0.65,
    "closing_qty": 11.85,
    "value": 414.75,
    "snapshot_at": "2026-05-31T00:00:00Z"
  }
]
```

---

## 6. Adjustments — `/api/v1/inventory/adjustments`

Manual stock corrections (not wastage, not transfers).

### `POST /api/v1/inventory/adjustments`  (perm: `inventory.update`)
```json
{
  "ingredient_id": "ing_a1b2c3",
  "branch_id": null,
  "adjustment_type": "increase",
  "quantity": 2.5,
  "unit": "kg",
  "unit_cost": 35.0,
  "reason": "Vendor delivered extra",
  "notes": "Bonus from supplier"
}
```
`adjustment_type` ∈ `increase | decrease | recount | damage | theft | found`.
Aliased field `direction` is also accepted for legacy clients.

Response `200`:
```json
{ "adjustment_id": "adj_01HX...", "event_id": "evt_01HX..." }
```

### `GET /api/v1/inventory/adjustments?ingredient_id=&limit=50`
```json
[
  {
    "id": "adj_01HX...",
    "restaurant_id": "...",
    "branch_id": "...",
    "ingredient_id": "ing_a1b2c3",
    "adjustment_type": "increase",
    "quantity": 2.5,
    "unit": "kg",
    "unit_cost": 35.0,
    "reason": "Vendor delivered extra",
    "notes": "Bonus from supplier",
    "ledger_event_id": "evt_01HX...",
    "created_by": "usr_owner",
    "created_at": "2026-05-31T10:01:00Z"
  }
]
```

---

## 7. Wastage — `/api/v1/inventory/wastage`

### `POST /api/v1/inventory/wastage`  (perm: `inventory.update`)
```json
{
  "ingredient_id": "ing_a1b2c3",
  "branch_id": null,
  "batch_id": null,
  "quantity": 0.65,
  "unit": "kg",
  "unit_cost": 35.0,
  "waste_reason": "spoilage",
  "notes": "Bruised in storage",
  "photo_url": "https://cdn.bittupos.com/wastage/abc.jpg"
}
```
`waste_reason` ∈ `spoilage | expiry | breakage | overcooked | customer_return | preparation_loss | contamination | other`.

Response `200`:
```json
{ "wastage_id": "wst_01HX...", "event_id": "evt_01HX..." }
```

### `GET /api/v1/inventory/wastage?ingredient_id=&limit=50`
Returns array of wastage rows with the same fields as the request plus
`id`, `restaurant_id`, `created_by`, `created_at`, `ledger_event_id`.

---

## 8. Stock transfers — `/api/v1/inventory/transfers`

Inter-branch ship → receive flow. State machine:
`draft → in_transit → received` (or `cancelled`).

### `POST /api/v1/inventory/transfers`  (perm: `inventory.update`)
```json
{
  "from_branch_id": "22222222-2222-2222-2222-222222222222",
  "to_branch_id":   "33333333-3333-3333-3333-333333333333",
  "items": [
    { "ingredient_id": "ing_a1b2c3", "quantity_sent": 5.0, "unit": "kg" },
    { "ingredient_id": "ing_p99",   "quantity_sent": 2.0, "unit": "L"  }
  ],
  "notes": "Weekly restock"
}
```
Response `200 { "transfer_id": "trn_01HX...", "status": "draft" }`.

### `POST /api/v1/inventory/transfers/{transfer_id}/ship`
No body. Debits FROM-branch. Response:
```json
{ "transfer_id": "trn_01HX...", "status": "in_transit" }
```

### `POST /api/v1/inventory/transfers/{transfer_id}/receive`
Body — `items` optional (omit to accept full sent qty):
```json
{
  "items": [
    { "ingredient_id": "ing_a1b2c3", "quantity_received": 5.0 },
    { "ingredient_id": "ing_p99",   "quantity_received": 1.8 }
  ]
}
```
Response `200 { "transfer_id": "...", "status": "received" }`.

### `GET /api/v1/inventory/transfers?status=&limit=50`
```json
[
  {
    "id": "trn_01HX...",
    "restaurant_id": "...",
    "from_branch_id": "...",
    "to_branch_id": "...",
    "status": "received",
    "requested_by": "usr_owner",
    "shipped_at": "2026-05-31T08:00:00Z",
    "received_at": "2026-05-31T11:15:00Z",
    "received_by": "usr_branch",
    "notes": "Weekly restock",
    "created_at": "2026-05-31T07:55:00Z"
  }
]
```

---

## 9. Physical counts — `/api/v1/inventory/counts`

### `POST /api/v1/inventory/counts/start`
```json
{ "branch_id": null, "count_type": "partial", "notes": "Friday cycle" }
```
`count_type` ∈ `full | partial | spot | cycle`. Response:
```json
{ "count_id": "cnt_01HX...", "count_number": "CNT-260531094501" }
```

### `POST /api/v1/inventory/counts/{count_id}/items`
One line per ingredient — call repeatedly as counter walks the shelf.
```json
{ "ingredient_id": "ing_a1b2c3", "counted_qty": 11.2, "unit": "kg" }
```
Response `200 { "ok": true }`.

### `POST /api/v1/inventory/counts/{count_id}/finalize`
No body. Emits `INVENTORY_RECOUNTED` events for every line whose variance
≠ 0 and locks the count.
```json
{ "count_id": "cnt_01HX...", "variances_applied": 4, "status": "approved" }
```

### `GET /api/v1/inventory/counts?limit=50`
```json
[
  {
    "id": "cnt_01HX...",
    "restaurant_id": "...",
    "branch_id": "...",
    "count_number": "CNT-260531094501",
    "count_type": "partial",
    "status": "approved",
    "count_date": "2026-05-31",
    "started_at": "2026-05-31T09:45:01Z",
    "completed_at": "2026-05-31T10:32:11Z",
    "approved_at": "2026-05-31T10:32:11Z",
    "started_by": "usr_owner",
    "approved_by": "usr_owner",
    "notes": "Friday cycle"
  }
]
```

### `GET /api/v1/inventory/counts/{count_id}`
```json
{
  "count": { "...same as list row..." },
  "items": [
    {
      "id": "cni_01HX...",
      "count_id": "cnt_01HX...",
      "ingredient_id": "ing_a1b2c3",
      "ingredient_name": "Tomato",
      "expected_qty": 12.5,
      "counted_qty": 11.2,
      "variance": -1.3,
      "unit": "kg",
      "unit_cost": 35.0,
      "counted_by": "usr_branch",
      "counted_at": "2026-05-31T10:01:00Z"
    }
  ]
}
```

---

## 10. Alerts — `/api/v1/inventory/alerts`

### `GET /api/v1/inventory/alerts?status=open&severity=&limit=50`
`status` ∈ `open | acknowledged | resolved`,
`severity` ∈ `low | medium | high | critical`.
```json
[
  {
    "id": "alr_01HX...",
    "restaurant_id": "...",
    "branch_id": "...",
    "ingredient_id": "ing_a1b2c3",
    "alert_type": "low_stock",
    "severity": "high",
    "status": "open",
    "title": "Tomato below reorder point",
    "message": "Current 4.2 kg, reorder at 5 kg",
    "current_qty": 4.2,
    "threshold_qty": 5.0,
    "created_at": "2026-05-31T07:00:00Z",
    "acknowledged_by": null,
    "acknowledged_at": null,
    "resolved_at": null
  }
]
```

### `POST /api/v1/inventory/alerts/{alert_id}/acknowledge`
No body → `{ "ok": true }`.

### `POST /api/v1/inventory/alerts/{alert_id}/resolve`
No body → `{ "ok": true }`.

---

## 11. Expiry dashboard — `/api/v1/inventory/expiry`

`GET /api/v1/inventory/expiry?bucket=&branch_id=`
`bucket` ∈ `expired | critical | warning | ok`.
```json
[
  {
    "ingredient_id": "ing_a1b2c3",
    "name": "Tomato",
    "batch_id": "btc_01HX...",
    "batch_number": "BTC-260520-01",
    "expiry_date": "2026-06-03",
    "days_to_expiry": 3,
    "expiry_bucket": "critical",
    "quantity_remaining": 4.2,
    "unit": "kg",
    "unit_cost": 35.0,
    "value_at_risk": 147.0,
    "branch_id": "..."
  }
]
```

---

## 12. Analytics — `/api/v1/inventory/analytics`

`GET /api/v1/inventory/analytics?ingredient_id=&days=30`
```json
[
  {
    "ingredient_id": "ing_a1b2c3",
    "name": "Tomato",
    "period_date": "2026-05-31",
    "qty_in": 5.0,
    "qty_out": 0.65,
    "value_in": 175.0,
    "value_out": 22.75,
    "wastage_qty": 0.65,
    "wastage_value": 22.75,
    "closing_balance": 11.85
  }
]
```

---

## 13. Vendors — `/api/v1/inventory/vendors`

### `GET /api/v1/inventory/vendors?active_only=true`
```json
[
  {
    "id": "ven_01HX...",
    "restaurant_id": "...",
    "name": "Green Farms",
    "contact_person": "Ravi K",
    "phone": "+919876543210",
    "email": "ravi@greenfarms.in",
    "address": "Plot 12, Sector 5",
    "city": "Hyderabad",
    "state": "TS",
    "pincode": "500032",
    "gst_number": "36ABCDE1234F1Z5",
    "pan_number": "ABCDE1234F",
    "payment_terms": 30,
    "credit_limit": 50000.0,
    "notes": "Daily delivery 6am",
    "is_active": true,
    "created_at": "2026-04-01T00:00:00Z"
  }
]
```

### `POST /api/v1/inventory/vendors`  (perm: `inventory.update`)
Request: same fields as above except `id`, `restaurant_id`, `is_active`,
`created_at`. Response `200 { "vendor_id": "ven_01HX..." }`.

### `PATCH /api/v1/inventory/vendors/{vendor_id}`
Any subset of the create body. Response: updated row.

---

## 14. Purchase orders — `/api/v1/purchase-orders`

Separate router. Required perm: `purchase_order.read` / `purchase_order.write`.

### `GET /api/v1/purchase-orders?status=&payment_status=&source_type=&limit=50&offset=0`
```json
[
  {
    "id": 7421,
    "po_number": "PO-260531-0007",
    "source_type": "supplier",
    "source_id": null,
    "source_name": "Green Farms",
    "supplier_name": "Green Farms",
    "supplier_contact": "+919876543210",
    "status": "draft",
    "payment_status": "unpaid",
    "expected_delivery_date": "2026-06-02",
    "delivery_time": "06:00:00",
    "delivery_charges": 50.0,
    "subtotal": 875.0,
    "tax_amount": 0.0,
    "total_amount": 925.0,
    "notes": "Weekly produce",
    "created_at": "2026-05-31T11:00:00Z",
    "items": [
      {
        "id": 9001,
        "ingredient_id": "ing_a1b2c3",
        "ingredient_name": "Tomato",
        "quantity_ordered": 25.0,
        "quantity_received": 0,
        "unit": "kg",
        "unit_price": 35.0,
        "line_total": 875.0
      }
    ]
  }
]
```

### `GET /api/v1/purchase-orders/{po_id}` → single row above.

### `POST /api/v1/purchase-orders`  (perm: `purchase_order.write`)
```json
{
  "source_type": "supplier",
  "source_id": null,
  "source_name": "Green Farms",
  "supplier_name": "Green Farms",
  "supplier_contact": "+919876543210",
  "status": "draft",
  "notes": "Weekly produce",
  "expected_delivery_date": "2026-06-02",
  "delivery_time": "06:00:00",
  "delivery_charges": 50.0,
  "payment_status": "unpaid",
  "items": [
    {
      "ingredient_id": "ing_a1b2c3",
      "ingredient_name": "Tomato",
      "quantity_ordered": 25.0,
      "unit": "kg",
      "unit_price": 35.0
    }
  ]
}
```
- `source_type` ∈ `supplier | restaurant | kitchen`.
- For `kitchen` set `source_id` to the kitchen-station id.
- Either `ingredient_id` or `ingredient_name` is required per line.

Response `201`: full PO row (same shape as list).

### `PATCH /api/v1/purchase-orders/{po_id}`
Any subset of the create body. If `items` is included the line set is
replaced atomically.

### `DELETE /api/v1/purchase-orders/{po_id}` → `{ "ok": true }`.

To convert an approved PO into stock use `POST /api/v1/inventory/receive`
(section 4).

---

## 15. WebSocket events

Subscribe via the existing `/ws` channel. Inventory mutations broadcast:

| Event topic              | Payload shape (subset)                                                         |
|--------------------------|--------------------------------------------------------------------------------|
| `INVENTORY_PURCHASED`    | `{ ingredient_id, branch_id, quantity_in, unit_cost, running_balance, reference_type }` |
| `INVENTORY_CONSUMED`     | `{ ingredient_id, branch_id, quantity_out, running_balance, reference_id }`    |
| `INVENTORY_WASTED`       | `{ ingredient_id, branch_id, quantity_out, metadata.waste_reason }`            |
| `INVENTORY_ADJUSTED`     | `{ ingredient_id, branch_id, quantity_in?, quantity_out?, metadata.adjustment_type }` |
| `INVENTORY_TRANSFERRED_OUT` | `{ ingredient_id, from_branch_id, quantity_out, reference_id }`             |
| `INVENTORY_TRANSFERRED_IN`  | `{ ingredient_id, to_branch_id, quantity_in, reference_id }`                |
| `INVENTORY_RECOUNTED`    | `{ ingredient_id, branch_id, quantity_in?, quantity_out?, reference_id }`      |
| `INVENTORY_LOW_STOCK`    | `{ alert_id, ingredient_id, severity, current_qty, threshold_qty }`            |

Use these to invalidate the local balances cache and refresh the timeline
without polling.

---

## 16. Error envelope

All inventory endpoints use the same FastAPI error shape:
```json
{
  "detail": "restaurant context required",
  "error_code": "validation_error",
  "trace_id": "cf1b2bee24ad0a88"
}
```
HTTP codes used: `400` validation, `401` missing/expired token, `403`
permission denied, `404` entity not found, `409` idempotency conflict,
`422` Pydantic body shape error, `500` server error.
