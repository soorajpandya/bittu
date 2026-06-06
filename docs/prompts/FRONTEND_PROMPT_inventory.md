# Frontend Prompt — Inventory (full surface)

> Companion to INVENTORY_FLUTTER_CONTRACT.md and inventory.md.
> This document is the **complete, exhaustive** list of backend endpoints the Flutter / React frontends must wire up to deliver feature-parity with the backend.
>
> **Rule of completeness:** if a backend route exists under one of the prefixes in §1, the FE has a screen, action, or background task for it. Nothing in the backend is "optional" or "internal-only".
>
> Auth: every endpoint requires `Authorization: Bearer <jwt>`. Tenant scope (`restaurant_id`, `branch_id`, `owner_id`) is derived from the JWT — **never** put `restaurant_id` in a request body or query string.

---

## 1. Routers in scope

| Prefix | File | Purpose |
|---|---|---|
| `/api/v1/inventory` | inventory.py | Event-sourced inventory: balances, ledger, snapshots, adjustments, wastage, transfers, counts, alerts, expiry, analytics, vendors, units, drift. |
| `/api/v1/ingredients` | ingredients.py | Ingredient master CRUD. |
| `/api/v1/item-ingredients` | item_ingredients.py | Recipe linking (menu item ↔ ingredient). |
| `/api/v1/ai-ingredients` | ai_ingredients.py | AI-suggested ingredients & auto-linking. |
| `/api/v1/purchase-orders` | purchase_orders.py | Purchase orders → received → stock-in. |
| WebSocket `/api/v1/realtime/ws` | __init__.py | `events:inventory.*` fan-out (see §21). |

---

## 2. Permissions

| Capability | Permission |
|---|---|
| View any inventory data | `inventory.read` |
| Adjustments / wastage / transfers / counts / vendor edits / unit edits | `inventory.update` |
| Force snapshot / reconciliation drift | `inventory.update` (admin role in practice) |
| Ingredient CRUD | `menu.write` (create/update), `menu.delete` (soft-delete) |
| Recipe link (item-ingredient) | `menu.read` / `menu.write` |
| Purchase order read | `purchase_order.read` |
| Purchase order write | `purchase_order.write` |
| Purchase order delete | `purchase_order.delete` |

Hide (don't disable) buttons the user lacks. Permission downgrade takes effect on next render — no re-login.

---

## 3. Idempotency & optimistic update protocol

Every mutating call MUST:

1. Generate a client UUIDv4 → sent as `Idempotency-Key: <uuid>` header **and** echoed in JSON body as `idempotency_key` where the schema accepts it.
2. Write to a local **outbox** (Drift/Isar on Flutter, IndexedDB on web) before any optimistic UI change.
3. Apply optimistic delta to local balance cache; show "syncing" badge.
4. On `2xx` → replace optimistic delta with server response / matching WS event; drop outbox row.
5. On `409 DUPLICATE_IDEMPOTENCY` → silent reconcile, no toast.
6. On `409 INSUFFICIENT_STOCK` / `422` → roll back delta, error toast.
7. On `5xx` / network → keep outbox row, exponential backoff retry.

**Strict FIFO per ingredient.** A recount-then-wastage sequence on the same ingredient must never be re-ordered by the outbox drainer.

Server dedups on `(restaurant_id, ingredient_id, dedup_key)` for ledger events; identical replays return the original `event_id`.

---

## 4. Ingredient master (`/api/v1/ingredients`)

Stock items are **created** and **named** here. Stock **quantity** changes never flow through here — use §10 / §11 / §12 / §13.

| Method | Path | Permission | Body / Query |
|---|---|---|---|
| GET | `` | uses `get_current_user` | `?include_inactive=bool` |
| POST | `` | `menu.write` | `IngredientIn` |
| PATCH | `/{ingredient_id}` | `menu.write` | `IngredientPatch` (partial) |
| DELETE | `/{ingredient_id}` | `menu.delete` | soft-delete; preserves ledger |

### 4.1 `IngredientIn`

```jsonc
{
  "name": "string (required, 1..120)",
  "unit": "string (default: 'unit')",
  "current_stock": 0,                 // if > 0, server appends an `opening` ledger event
  "reorder_point": null,
  "reorder_quantity": null,
  "minimum_stock": null,
  "cost_per_unit": null,              // ₹ per unit
  "category": null,
  "storage_location": null,           // e.g. "Cold Room A"
  "storage_type": "dry",              // dry | cold | frozen | dairy | produce
  "is_perishable": false,
  "shelf_life_days": null,
  "track_batches": false,
  "sku": null,
  "barcode": null,
  "supplier": null,
  "branch_id": null
}
```

### 4.2 Create rules

- **Idempotent on `(restaurant_id, lower(name))`**. Re-POSTing the same name returns existing row with `created: false` — treat as success.
- If `current_stock > 0`, server appends an opening event with `dedup_key = "opening:{ingredient_id}"`. Do NOT also POST an adjustment for the opening qty.
- After create/update → invalidate `/inventory/balances` cache.

### 4.3 Delete behaviour

Soft-delete (`deleted_at = NOW()`, `is_active = false`). Ledger preserved. Restore is not API-exposed — copy: "Once removed, the item is hidden everywhere." Hide soft-deleted ingredients in pickers.

---

## 5. Recipe linking (`/api/v1/item-ingredients`)

| Method | Path | Permission | Body |
|---|---|---|---|
| GET | `?item_id=<int>` | `menu.read` | list links for one item (or all) |
| POST | `` | `menu.write` | `{ item_id:int, ingredient_id:int|string, quantity_used:float, unit?:string }` |
| PATCH | `/{ii_id}` | `menu.write` | `{ quantity_used?, unit? }` |
| DELETE | `/{ii_id}` | `menu.write` | unlink |

- "Recipe" tab on menu-item editor: ingredient picker (from `/ingredients`), quantity, unit.
- `ingredient_id` may be int or string — send what the picker returned.
- After any change, refresh the item; if it's in any open order, toast: "Recipe updates apply to future orders only."

---

## 6. AI ingredient helpers (`/api/v1/ai-ingredients`)

| Method | Path | Body | Purpose |
|---|---|---|---|
| POST | `/suggest` | `{ "item_name": "Paneer Tikka" }` | Returns suggestions **without saving** |
| POST | `/auto-link` | `{ "item_id": 42, "item_name": "Paneer Tikka" }` | Suggests + matches/creates ingredients + writes `item_ingredients` rows |

- "Suggest" → render as draft chips with checkboxes; user confirms before POST `/item-ingredients`.
- "Auto-link" → confirm dialog. After success, refetch `/item-ingredients?item_id=` and `/ingredients`.
- Show soft-loading — LLM round-trip is 3–8s.

---

## 7. Legacy stock surface (`/api/v1/inventory`)

Kept for back-compat; do not remove from FE.

| Method | Path | Permission | Use |
|---|---|---|---|
| GET | `/stock` | `inventory.read` | `?branch_id=&low_only=bool` — returns master `current_stock` |
| POST | `/receive` | `inventory.update` | `{ "purchase_order_id": "<uuid>" }` — receives a PO, writes stock-in events |

Prefer `/balances` (§8) for live data. Use `/stock` only on the legacy "Stock" tile.

---

## 8. Event-sourced balances & timeline

| Method | Path | Query | Returns |
|---|---|---|---|
| GET | `/api/v1/inventory/balances` | `?branch_id=` | array of `{ingredient_id, name, unit, current_qty, reorder_point, last_event_at, status}` |
| GET | `/api/v1/inventory/balance/{ingredient_id}` | `?branch_id=&as_of=ISO` | `{ingredient_id, balance, as_of}` |
| GET | `/api/v1/inventory/timeline/{ingredient_id}` | `?branch_id=&limit<=500&offset=0` | paginated ledger events |

`status` ∈ `ok | low | out | negative`.

**Cache rules**
- Balances list: read-through against Isar/IndexedDB. Hydrate locally, refresh from server, merge.
- Single ingredient: memoize 30s by `(ingredientId, branchId)`. Bust on any `INVENTORY_*` WS event.
- Timeline: append-only paginated; cache last 50 per ingredient.

---

## 9. Snapshots (`/inventory/snapshots`)

| Method | Path | Permission | Use |
|---|---|---|---|
| GET | `/snapshots` | `inventory.read` | `?ingredient_id=&branch_id=&period=rolling&limit<=200` |
| POST | `/snapshots/build` | `inventory.update` | `?branch_id=&period=rolling` |

Only managers see **Force snapshot** on the Reconciliation screen. Snapshots are a *report* — never wire them into the dashboard.

---

## 10. Adjustments (`/inventory/adjustments`)

| Method | Path | Body |
|---|---|---|
| POST | `/adjustments` | `AdjustmentIn` |
| GET | `/adjustments` | `?ingredient_id=&limit<=200` |

### 10.1 `AdjustmentIn`

```jsonc
{
  "ingredient_id": "ing_…",
  "branch_id": null,                // null → caller's branch
  "adjustment_type": "increase",    // increase | decrease | recount | damage | theft | found
  "quantity": "1.500",              // string-safe decimal, > 0
  "unit": "kg",
  "unit_cost": null,
  "reason": "free text",
  "notes": "free text"
}
```

Legacy clients may send `direction` (alias of `adjustment_type`). New FE code MUST use `adjustment_type`.

`increase`/`found` add stock; `decrease`/`damage`/`theft` subtract. Show "+1.5 kg" / "-1.5 kg" preview before submit. 2xx response: `{ adjustment_id, event_id }`.

---

## 11. Wastage (`/inventory/wastage`)

| Method | Path | Body |
|---|---|---|
| POST | `/wastage` | `WastageIn` |
| GET | `/wastage` | `?ingredient_id=&limit<=200` |

### 11.1 `WastageIn`

```jsonc
{
  "ingredient_id": "ing_…",
  "branch_id": null,
  "batch_id": null,                 // when ingredient.track_batches
  "quantity": "0.250",              // > 0
  "unit": "kg",
  "unit_cost": null,
  "waste_reason": "spoilage",       // spoilage | expiry | breakage | overcooked
                                    // | customer_return | preparation_loss
                                    // | contamination | other
  "notes": null,
  "photo_url": null                 // upload via /files first, then attach URL
}
```

Quick-log FAB on dashboard + ingredient detail. Photo optional — upload first, then submit with the URL. After 2xx update balance: `qty -= quantity`. Toast with "Undo" → links to Adjustments (no auto-reverse).

---

## 12. Stock transfers (`/inventory/transfers`)

Two-step (ship debits source, receive credits destination). Each leg is dedup-keyed.

| Method | Path | Use |
|---|---|---|
| POST | `/transfers` | Create draft |
| GET | `/transfers` | `?status=draft|in_transit|received|cancelled&limit<=200` |
| POST | `/transfers/{id}/ship` | Emits `INVENTORY_TRANSFERRED_OUT` per line |
| POST | `/transfers/{id}/receive` | Emits `INVENTORY_TRANSFERRED_IN` per line |

### 12.1 Create body

```jsonc
{
  "from_branch_id": "<uuid>",
  "to_branch_id":   "<uuid>",
  "notes": null,
  "items": [ { "ingredient_id": "ing_…", "quantity_sent": "1.500", "unit": "kg" } ]
}
```

Backend rejects same-branch (`400`); FE must also block in picker.

### 12.2 Receive body

```jsonc
{
  "items": [   // optional. omit → received = sent for every line
    { "ingredient_id": "ing_…", "quantity_received": "1.450" }
  ]
}
```

### 12.3 FE rules

- Status flow `draft → in_transit → received` rendered as a stepper.
- **Ship** enabled only on source branch + `inventory.update`.
- **Receive** enabled only on destination branch + `inventory.update`. Force per-line confirmation; default `quantity_received = quantity_sent`, allow edit. Variance is normal and silently recorded.
- Backend dedups on `transfer:{id}:out:{ingredient_id}` / `…:in:…`. FE still sends `Idempotency-Key` at HTTP layer.

---

## 13. Physical counts (`/inventory/counts`)

| Method | Path | Use |
|---|---|---|
| POST | `/counts/start` | Begin session; snapshots `expected_qty` per ingredient |
| POST | `/counts/{id}/items` | Upsert one counted line. Call repeatedly while staff scans |
| POST | `/counts/{id}/finalize` | Emits `INVENTORY_RECOUNTED` per non-zero variance |
| GET | `/counts` | `?limit<=200` recent |
| GET | `/counts/{id}` | Detail incl. items + variances |

### 13.1 Start body

```jsonc
{ "branch_id": null, "count_type": "partial",   // full | partial | spot | cycle
  "notes": null }
```

Response: `{ "count_id", "count_number": "CNT-260528123010" }`.

### 13.2 Submit-item body

```jsonc
{ "ingredient_id": "ing_…", "counted_qty": "12.300", "unit": "kg" }
```

### 13.3 Non-negotiable FE rules

- **Persist every keystroke** locally. App kill mid-count must lose zero entries.
- Outbox FIFO per ingredient. 20-min wifi outage → all lines reconcile, no dupes.
- Variance > 5% triggers a server-side alert (§14) — refetch `/alerts` after finalize.
- Finalize: `{ count_id, status: "approved", variances_applied: N }`.
- After finalize, **invalidate the entire balances cache** for the branch.

---

## 14. Alerts (`/inventory/alerts`)

| Method | Path | Use |
|---|---|---|
| GET | `/alerts` | `?status=open|acknowledged|resolved&severity=&limit<=200` |
| POST | `/alerts/{id}/acknowledge` | mark ack'd |
| POST | `/alerts/{id}/resolve` | mark resolved |

- Inbox grouped by type. Tap → ingredient detail with alert highlighted.
- Swipe-right = ack; swipe-left = resolve. Both idempotent.
- Severity pills: `info` gray, `warning` amber, `critical` red.
- Bottom-nav "Inventory" badge = count of `status=open && severity ∈ {warning, critical}`.

---

## 15. Expiry dashboard (`/inventory/expiry`)

| Method | Path | Query |
|---|---|---|
| GET | `/expiry` | `?bucket=expired|critical|warning|ok&branch_id=` |

Render 4-tab view. Tapping a row opens Wastage form pre-filled with the batch's qty + reason `expiry`.

---

## 16. Analytics (`/inventory/analytics`)

| Method | Path | Query |
|---|---|---|
| GET | `/analytics` | `?ingredient_id=&days=1..365` (default 30) |

Returns daily rollups from `inventory_analytics`: opening / in / out / wastage / closing / COGS per ingredient per day.

**Charts**
- COGS today / week / month (sum rows).
- Top 5 most-consumed (sort by `total_out`).
- Wastage % (`wastage_qty / total_out`).
- Vendor mix (join with PO data).
- **Never aggregate client-side** if the view already rolled up — pass `days=` and accept the response.

---

## 17. Vendors (`/inventory/vendors`)

| Method | Path | Permission | Body |
|---|---|---|---|
| GET | `/vendors` | `inventory.read` | `?active_only=bool` |
| POST | `/vendors` | `inventory.update` | `VendorIn` |
| PATCH | `/vendors/{vendor_id}` | `inventory.update` | `VendorIn` (**full**, not partial) |
| POST | `/vendors/{vendor_id}/toggle` | `inventory.update` | flip `is_active` |

### 17.1 `VendorIn`

```jsonc
{
  "name": "string (required)",
  "contact_person": null, "phone": null, "email": null,
  "address": null, "city": null, "state": null, "pincode": null,
  "gst_number": null, "pan_number": null,
  "payment_terms": 30,       // days
  "credit_limit": "0",       // ₹
  "notes": null
}
```

PATCH sends the **full** body (matches backend SQL). If FE has partial-edit UX, merge with the previously fetched record. Toggle returns `{ vendor_id, is_active }` — update in place, don't refetch the list.

---

## 18. Unit conversions (`/inventory/units/conversions`)

| Method | Path | Use |
|---|---|---|
| GET | `/units/conversions` | All active conversions (global + restaurant-scoped) |
| POST | `/units/conversions` | Upsert; idempotent on `(restaurant_id, ingredient_id, from_unit, to_unit)` |

```jsonc
{
  "ingredient_id": null,    // null → restaurant-wide rule
  "from_unit": "kg", "to_unit": "g",
  "factor": "1000"          // > 0
}
```

Settings → Inventory → Unit Conversions. Inline edit. No DELETE endpoint — don't expose delete. Use this map to render qty in a user's preferred unit anywhere the ingredient is shown.

---

## 19. Reconciliation drift (`/inventory/reconciliation/drift`)

| Method | Path | Use |
|---|---|---|
| GET | `/reconciliation/drift` | Ingredients where `ingredients.current_stock` ≠ `Σ ledger` |

Manager-only screen under Inventory → Settings → "Reconcile". Each row: name, master qty, ledger qty, drift. Per-row actions:

1. **Force snapshot** → `POST /snapshots/build`.
2. **Manual adjustment** → opens Adjustments sheet pre-filled with `quantity = abs(drift)`, `adjustment_type = increase` if drift > 0 else `decrease`, `reason = "reconciliation"`.

After action, refetch `/reconciliation/drift`; success only when the row disappears.

---

## 20. Purchase orders (`/api/v1/purchase-orders`)

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `` | `purchase_order.read` | `?status=&payment_status=&source_type=&limit=&offset=` |
| GET | `/{po_id}` | `purchase_order.read` | full PO with items |
| POST | `` | `purchase_order.write` | `POCreate` |
| PATCH | `/{po_id}` | `purchase_order.write` | `POUpdate` (partial) |
| DELETE | `/{po_id}` | `purchase_order.delete` | only when status=draft |

### 20.1 `POCreate`

```jsonc
{
  "source_type": "supplier",        // supplier | restaurant | kitchen
  "source_id": null,                // kitchen_station.id when source_type=kitchen
  "source_name": null,
  "supplier_name": null,            // back-compat
  "supplier_contact": null,
  "status": "draft",                // draft | ordered | received | cancelled
  "notes": null,
  "expected_delivery_date": "2026-05-30",
  "delivery_time": "14:30:00",
  "delivery_charges": 0,
  "payment_status": "unpaid",       // unpaid | paid
  "items": [
    {
      "ingredient_id": "ing_…",     // OR ingredient_name for ad-hoc lines
      "ingredient_name": null,
      "quantity_ordered": 5, "unit": "kg", "unit_price": 320
    }
  ]
}
```

### 20.2 Flow

1. Create PO in `draft` → review → set `status=ordered`.
2. When goods arrive:
   - **Quick path:** `POST /api/v1/inventory/receive { purchase_order_id }`. Backend marks received + writes `INVENTORY_PURCHASED`.
   - **Detailed path:** PATCH PO to `status=received` with per-line `quantity_received`. Backend also writes events on this transition.
3. Refresh `/balances` after receive.

### 20.3 Guards

- Delete blocked unless `status == draft`.
- Items not editable once `status == received`.
- `payment_status` is independent of receive — Accounts Payable, not Inventory.

---

## 21. Realtime — WebSocket events

Connect to `/api/v1/realtime/ws`. Subscribe to `branch:{branch_id}` and `restaurant:{restaurant_id}`.

| Constant | `event_type` | Effect |
|---|---|---|
| `INVENTORY_PURCHASED` | `inventory.purchased` | balance += qty_in |
| `INVENTORY_CONSUMED` | `inventory.consumed` | balance -= qty_out |
| `INVENTORY_WASTED` | `inventory.wasted` | balance -= qty_out |
| `INVENTORY_EXPIRED` | `inventory.expired` | balance -= qty_out |
| `INVENTORY_TRANSFERRED_OUT` | `inventory.transferred_out` | source balance -= |
| `INVENTORY_TRANSFERRED_IN` | `inventory.transferred_in` | dest balance += |
| `INVENTORY_ADJUSTED` | `inventory.adjusted` | apply qty_in − qty_out |
| `INVENTORY_RECOUNTED` | `inventory.recounted` | apply qty_in − qty_out |
| `INVENTORY_RETURN_TO_VENDOR` | `inventory.return_to_vendor` | balance -= qty_out |
| `INVENTORY_RESTOCK_CANCELLED` | `inventory.restock_cancelled_order` | balance += qty_in |
| `INVENTORY_DEDUCTED` | `inventory.deducted` | legacy alias of consumed |
| `INVENTORY_RESTORED` | `inventory.restored` | legacy alias of restock |
| `INVENTORY_LOW_STOCK` | `inventory.low_stock` | refetch `/alerts`; toast |
| `INVENTORY_OUT_OF_STOCK` | `inventory.out_of_stock` | refetch `/alerts`; toast |
| `INVENTORY_NEGATIVE_STOCK` | `inventory.negative_stock` | refetch `/alerts`; **red banner** |
| `INVENTORY_BATCH_EXPIRING` | `inventory.batch_expiring` | refetch `/expiry`; refetch `/alerts` |
| `INVENTORY_ALERT_RAISED` | `inventory.alert_raised` | refetch `/alerts`; bump nav badge |
| `INVENTORY_SNAPSHOT_BUILT` | `inventory.snapshot_built` | refetch `/snapshots` |
| `INVENTORY_RECONCILIATION_DRIFT` | `inventory.reconciliation_drift` | refetch drift; managers only |

**Dedup ring buffer:** keep last 256 `event_id`s; ignore dupes. Optimistic deltas already applied locally must be **replaced** (not added) when the matching WS event arrives.

**Reconnect:** exponential backoff 1s→2s→5s→10s→cap 30s. Send `{"type":"resume","since":"<lastEventId>"}`. If server returns `out_of_window`, full `/balances` refresh.

---

## 22. Error mapping

| HTTP | code / detail | UX |
|---|---|---|
| 400 | `from_branch and to_branch must differ` | inline transfer form error |
| 400 | `name required` (ingredients) | inline field error |
| 401 | any | redirect to login |
| 403 | permission denied | hide button; if reached, toast "No access" |
| 404 | `ingredient/stock_transfer/inventory_count/vendor not found` | toast + back-nav or refetch |
| 409 | `DUPLICATE_IDEMPOTENCY` | silent reconcile |
| 409 | `INSUFFICIENT_STOCK` | toast + rollback |
| 422 | validation | inline field errors from `detail[]` |
| 423 | `LEDGER_LOCKED` | spin 500ms + retry once |
| 5xx / net | any | banner + outbox holds mutation |

---

## 23. Screens checklist (parity)

- [ ] **Inventory dashboard** — balances list, branch + status filters, pull-to-refresh.
- [ ] **Ingredient detail** — header card, tabs: Timeline / Batches / Vendors / Alerts.
- [ ] **Ingredient master** — list, create, edit, soft-delete confirm.
- [ ] **Recipe editor on menu item** — list/add/edit/remove links.
- [ ] **AI suggest / auto-link** — buttons on menu item editor.
- [ ] **Adjustment sheet** — full enum, +/- preview.
- [ ] **Wastage sheet** — reason enum, optional batch, optional photo upload.
- [ ] **Transfers list + detail** — stepper, ship gate, receive gate.
- [ ] **New transfer** — branch picker, multi-line items.
- [ ] **Counts list + detail** — start, scan/input, finalize.
- [ ] **Count session** — local persistence + outbox + variance highlight.
- [ ] **Alerts inbox** — filters, ack/resolve swipes, nav badge.
- [ ] **Expiry dashboard** — 4 buckets, jump to Wastage prefilled.
- [ ] **Analytics** — COGS, top-5, wastage %, vendor mix.
- [ ] **Vendors** — list, create, edit, toggle.
- [ ] **Unit conversions** — settings table, inline upsert.
- [ ] **Reconciliation drift** — manager-only, force snapshot, manual fix.
- [ ] **Purchase orders** — list, detail, create, edit, receive, delete (draft only).
- [ ] **Legacy stock tile** — back-compat (`/inventory/stock`, `/inventory/receive`).

---

## 24. Acceptance checklist

- [ ] Every endpoint in §1 has a screen or background call.
- [ ] Every mutation sends `Idempotency-Key` header.
- [ ] Every mutation flows through the outbox.
- [ ] WS subscription established on app start, resubscribed on reconnect with `since:<lastEventId>`.
- [ ] Optimistic deltas are always **replaced** (never added) when the matching WS event arrives.
- [ ] Permissions hide (not just disable) admin-only actions.
- [ ] 200-item count survives intermittent connectivity with zero data loss.
- [ ] App-kill mid-mutation does not drop outbox row.
- [ ] Drift screen only renders for users with `inventory.update`.
- [ ] No request body or query contains `restaurant_id` (always JWT-derived).

---

## 25. Out of scope (do NOT implement on FE)

- Direct writes to `inventory_ledger` — no such endpoint; go through adjustments / wastage / transfers / counts.
- Restore of soft-deleted ingredients — admin-only via SQL.
- Hard delete of any inventory event — ledger is append-only.
- Manual edits to `ingredients.current_stock` — mirrored by backend from ledger; FE never PATCHes it.
