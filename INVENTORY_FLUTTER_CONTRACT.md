# Bittu POS — Inventory Flutter Contract (Section 8)

> Companion to [inventory.md](inventory.md). Authoritative API + realtime
> contract for the Flutter app. All endpoints are mounted under
> `/api/v1/inventory` and require the standard `Authorization: Bearer <jwt>`
> header. Tenant scoping (`restaurant_id`, `branch_id`) is derived from
> the JWT — clients **must not** send `restaurant_id` in payloads.
>
> Architecture rule: the backend is the single source of truth. Flutter
> caches are read-through; writes are optimistic but must be **reconciled**
> against the server response or the realtime broadcast (whichever lands
> first). Idempotency keys are mandatory on every mutation.

---

## 1. State management & layering

```
┌──────────────────────────────────────────────────────────┐
│  UI widgets (Riverpod ConsumerWidget / BlocBuilder)      │
├──────────────────────────────────────────────────────────┤
│  Notifier / Cubit layer  (InventoryBalancesNotifier,     │
│   InventoryAlertsNotifier, InventoryCountNotifier, …)    │
├──────────────────────────────────────────────────────────┤
│  Repository  (InventoryRepository — single instance)     │
│   ├─ REST client  (Dio + retry + idempotency interceptor)│
│   ├─ WS client    (events:* fan-out)                     │
│   └─ Local cache  (Isar / Drift — read-through only)     │
├──────────────────────────────────────────────────────────┤
│  Outbox queue (Drift) — offline mutations w/ idem-key    │
└──────────────────────────────────────────────────────────┘
```

Recommended packages: `flutter_riverpod`, `dio`, `retrofit`, `isar`,
`web_socket_channel`, `freezed`, `json_serializable`, `uuid`,
`connectivity_plus`.

**Single repository per restaurant session.** Dispose on logout/branch
switch to drop the WS subscription and local cache.

---

## 2. Cache strategy

| Concern             | Strategy                                                                 |
| ------------------- | ------------------------------------------------------------------------ |
| Balances list       | Read-through. Hydrate from Isar, refresh from `/balances`, then merge.   |
| Single ingredient   | Memo by `(ingredientId, branchId)` for 30 s; bust on WS event.           |
| Timeline            | Append-only paginated list. Cache last 50 events per ingredient.         |
| Alerts              | Local store mirrored from server; WS upserts; resolved alerts pruned.    |
| Counts in progress  | Persist locally — survives app restart so a count is never lost.         |
| Adjustments/wastage | Write-through outbox. Never cached for read (always fresh from server).  |

**Cache invalidation rule:** any inbound WS event of type `INVENTORY_*`
for an ingredient invalidates that ingredient's balance cache and forces
a re-fetch on next access (or a soft refresh if the screen is visible).

---

## 3. Optimistic update protocol

Every mutating call follows this pattern:

```dart
Future<Result<T>> mutate({required String idemKey, required Future<T> Function() call}) async {
  // 1. Apply optimistic delta to local cache.
  // 2. Enqueue in outbox with idemKey.
  // 3. Try server call.
  // 4a. On 2xx → confirm delta, drop outbox row.
  // 4b. On 409/422 → roll back delta, surface error.
  // 4c. On network error → keep outbox row; retry on connectivity restore.
}
```

* **Idempotency key**: client-generated UUIDv4, sent as `Idempotency-Key`
  header AND echoed in the JSON body's `idempotency_key` field where the
  endpoint accepts one. The backend dedups on `(restaurant_id, idem_key)`.
* **Reconciliation**: when a WS event arrives carrying the same
  `event_id` the server returned, the optimistic delta is replaced by
  the authoritative event payload. Never apply a WS delta twice — keep a
  ring buffer of the last 256 `event_id`s seen.

---

## 4. REST contract

All responses are JSON. Errors follow the existing API shape:

```json
{ "detail": "human readable", "code": "MACHINE_CODE" }
```

Pagination params (where supported): `?limit=50&offset=0`.
Date params are ISO-8601 UTC (`2026-05-10T12:00:00Z`).

### 4.1 Balances & timeline

| Method | Path                                  | Purpose                                                |
| ------ | ------------------------------------- | ------------------------------------------------------ |
| GET    | `/balances`                           | All ingredient balances for current branch.            |
| GET    | `/balance/{ingredient_id}`            | Single ingredient (optionally `?as_of=ISO`).           |
| GET    | `/timeline/{ingredient_id}`           | Paginated event stream for one ingredient.             |
| GET    | `/snapshots`                          | List of materialised period snapshots.                 |
| POST   | `/snapshots/build`                    | Force a snapshot rebuild (manager only).               |

**`GET /balances` response**

```json
{
  "items": [
    {
      "ingredient_id": "ing_abc",
      "name": "Paneer",
      "unit": "kg",
      "current_qty": 12.4,
      "reorder_point": 5.0,
      "last_event_at": "2026-05-10T11:42:11Z",
      "status": "ok"            // ok | low | out | negative
    }
  ],
  "as_of": "2026-05-10T12:00:00Z"
}
```

**`GET /timeline/{id}` response**

```json
{
  "items": [
    {
      "event_id": "evt_…",
      "type": "consumption",
      "quantity_in": 0,
      "quantity_out": 0.25,
      "unit_cost": 320.0,
      "reference_type": "order",
      "reference_id": "ord_123",
      "occurred_at": "2026-05-10T11:42:11Z",
      "reversed_by": null,
      "metadata": { "order_item_id": "oi_…" }
    }
  ],
  "next_cursor": "MTcxNTM0…"
}
```

### 4.2 Adjustments & wastage

| Method | Path                | Body                                                                               |
| ------ | ------------------- | ---------------------------------------------------------------------------------- |
| POST   | `/adjustments`      | `{ ingredient_id, delta, reason, notes, idempotency_key }` → `409` on duplicate.   |
| GET    | `/adjustments`      | `?from=…&to=…&ingredient_id=…`                                                     |
| POST   | `/wastage`          | `{ ingredient_id, quantity, reason, batch_id?, notes, idempotency_key }`           |
| GET    | `/wastage`          | `?from=…&to=…`                                                                     |

`reason` enum (UI dropdown):
`damaged`, `expired`, `quality_issue`, `over_prep`, `customer_return`, `staff_error`, `other`.

### 4.3 Transfers

| Method | Path                              | Purpose                                                              |
| ------ | --------------------------------- | -------------------------------------------------------------------- |
| POST   | `/transfers`                      | Create draft transfer with line items.                               |
| GET    | `/transfers`                      | List, filter `?status=draft|in_transit|received|cancelled`.          |
| POST   | `/transfers/{id}/ship`            | Mark shipped (writes `transfer_out` events on source branch).        |
| POST   | `/transfers/{id}/receive`         | Mark received (writes `transfer_in` events on destination branch).   |

The Flutter UI **must** disable the "Receive" button until the user is
on the destination branch and has `inventory.update` permission.

### 4.4 Counts (physical stock take)

| Method | Path                          | Purpose                                                                             |
| ------ | ----------------------------- | ----------------------------------------------------------------------------------- |
| POST   | `/counts/start`               | Begins a count session, snapshots `expected_qty` from `fn_inventory_balance`.       |
| POST   | `/counts/{id}/items`          | Bulk upsert counted quantities. Safe to call repeatedly while staff scans.          |
| POST   | `/counts/{id}/finalize`       | Commits variances as `recount` events; writes alerts on deltas > 5 %.               |
| GET    | `/counts`                     | List recent counts.                                                                 |
| GET    | `/counts/{id}`                | Detail with all items + variance.                                                   |

UI rule: the count screen **must** persist locally on every keystroke
and queue line writes through the outbox. A staff member losing wifi
mid-count must not lose a single counted item.

### 4.5 Alerts

| Method | Path                          | Purpose                                                  |
| ------ | ----------------------------- | -------------------------------------------------------- |
| GET    | `/alerts`                     | `?status=open|ack|resolved&type=…`                       |
| POST   | `/alerts/{id}/ack`            | Acknowledge.                                             |
| POST   | `/alerts/{id}/resolve`        | Resolve with `{ resolution_notes }`.                     |

### 4.6 Other

| Method | Path                              | Purpose                                  |
| ------ | --------------------------------- | ---------------------------------------- |
| GET    | `/expiry`                         | Items expiring within N days.            |
| GET    | `/analytics`                      | Top consumed, wastage %, COGS rollups.   |
| GET    | `/vendors` / POST / PATCH         | Vendor master.                           |
| GET    | `/units/conversions` / POST       | Unit conversion table.                   |
| GET    | `/reconciliation/drift`           | Mirror-master drift report (manager).    |

### 4.7 Legacy (kept for back-compat — DO NOT use in new screens)

* `POST /inventory/stock` — direct master mutation. Will be removed in
  Phase 4.
* `POST /inventory/receive` — direct receive. Use purchase-order flow
  via ERP module instead.

---

## 5. Realtime contract

Single WebSocket per session:

```
wss://api/realtime?token=<jwt>
```

Server fan-out channels (subscribe by joining; auto-subscribed by JWT):

* `branch:{branch_id}` — every inventory event for the branch.
* `restaurant:{restaurant_id}` — cross-branch rollups, snapshot built.
* `entity:ingredient:{ingredient_id}` — focused ingredient updates.
* `user:{user_id}` — alert escalations targeted at the user.

### Frame shape

```json
{
  "type": "INVENTORY_CONSUMED",
  "id": "evt_…",
  "restaurant_id": "rst_…",
  "branch_id": "brc_…",
  "occurred_at": "2026-05-10T11:42:11Z",
  "payload": {
    "event_id": "evt_…",
    "ingredient_id": "ing_…",
    "quantity_in": 0,
    "quantity_out": 0.25,
    "unit_cost": 320.0,
    "reference_type": "order",
    "reference_id": "ord_123",
    "balance_after": 12.15
  }
}
```

### Subscribe matrix

| Screen                 | Channels needed                                         |
| ---------------------- | ------------------------------------------------------- |
| Inventory dashboard    | `branch:{b}` + `INVENTORY_ALERT_RAISED`                 |
| Ingredient detail      | `entity:ingredient:{id}` + alerts for that ingredient   |
| Counts in progress     | `branch:{b}` filtered to `INVENTORY_RECOUNTED`          |
| Transfers list         | `branch:{b}` filtered to `INVENTORY_TRANSFERRED_*`      |
| Alerts inbox           | `branch:{b}` + `user:{me}`                              |

### Event types Flutter must handle

```
INVENTORY_PURCHASED            INVENTORY_TRANSFERRED_OUT
INVENTORY_CONSUMED             INVENTORY_TRANSFERRED_IN
INVENTORY_WASTED               INVENTORY_ADJUSTED
INVENTORY_EXPIRED              INVENTORY_RECOUNTED
INVENTORY_RETURN_TO_VENDOR     INVENTORY_RESTOCK_CANCELLED
INVENTORY_OUT_OF_STOCK         INVENTORY_NEGATIVE_STOCK
INVENTORY_BATCH_EXPIRING       INVENTORY_ALERT_RAISED
INVENTORY_SNAPSHOT_BUILT       INVENTORY_RECONCILIATION_DRIFT
```

Behavior rules:

* `INVENTORY_*` event with `ingredient_id` → patch the row in the
  balances list, recompute `status` from new balance.
* `INVENTORY_ALERT_RAISED` → push toast + insert into alerts inbox;
  vibrate on critical (`out_of_stock`, `negative_stock`).
* `INVENTORY_SNAPSHOT_BUILT` → refresh analytics screen if open.
* `INVENTORY_RECONCILIATION_DRIFT` → managers only, navigate to
  reconciliation screen.

### Reconnection

* Exponential backoff: 1 s → 2 s → 5 s → 10 s → cap 30 s.
* On successful reconnect, send `{"type":"resume","since":"<lastEventId>"}`.
  Server replays any missed events from the Redis stream window
  (configured 15 min). If `since` is older than the window, client must
  do a full `/balances` refresh.

---

## 6. Offline-safe mutation flow

```
[ user taps "Mark wastage 0.5 kg" ]
        │
        ▼
1. Generate idemKey = uuidv4
2. Local Isar: write pending event into inventory_outbox
3. Optimistic patch balance (-0.5)
4. Show row with subtle "syncing" badge
        │
        ▼
[ outbox worker drains FIFO when connectivity is up ]
        │
        ▼
POST /wastage { …, idempotency_key: idemKey }
        │
   ┌────┴────┐
   │ 2xx     │ → mark outbox row done, replace optimistic delta with
   │         │   server event, clear badge
   │ 409 dup │ → server already processed, fetch event by idemKey,
   │         │   reconcile, clear badge
   │ 4xx err │ → roll back delta, show error toast, mark outbox failed
   │ 5xx/net │ → keep in outbox, retry with backoff
   └─────────┘
```

Outbox draining order: **strict FIFO per ingredient** so a
recount-then-wastage sequence is never re-ordered on the server.

---

## 7. Screens & flows

### 7.1 Inventory Dashboard (`/inventory`)

* Sticky header: branch chip, search, filter (status), date range.
* Body: virtualized list of balances. Each row:
  `{name} · {qty} {unit} · status pill · last event time`.
* FAB: + Adjustment / + Wastage / + Count.
* Pull-to-refresh forces `/balances` re-fetch.

### 7.2 Ingredient Detail (`/inventory/:id`)

* Top card: current qty, reorder point, projected runway (days based on
  7-day moving consumption from `/analytics`).
* Tabs: **Timeline** · **Batches** · **Vendors** · **Alerts**.
* Timeline: infinite scroll, sticky day separators, color-coded by
  event type, swipe-left on a manual event reveals "reverse".

### 7.3 Stock Count (`/inventory/counts/:id`)

* Two-pane on tablet, single-pane on phone.
* Each line: expected qty (read-only), counted qty (numeric input),
  variance auto-computed, note icon.
* Bottom bar: progress (24 / 137 counted), Save, Finalize.
* Local persistence on every digit. Outbox drains in background.

### 7.4 Transfers (`/inventory/transfers`)

* List with status chips. Detail shows source/dest branches, items,
  ship/receive timeline.
* Receive screen forces per-line `quantity_received` confirmation —
  defaults to `quantity_sent` but is editable (variance triggers an
  adjustment event server-side).

### 7.5 Alerts inbox (`/inventory/alerts`)

* Grouped by type. Tap → ingredient detail with the alert highlighted.
* Swipe right: ack. Swipe left: resolve (prompts for note).

### 7.6 Analytics (`/inventory/analytics`)

* KPIs: COGS today/week/month, wastage %, top 5 consumed, vendor mix.
* All charts driven by `/analytics` — no client-side aggregation.

### 7.7 Reconciliation (`/inventory/reconciliation`) — managers only

* Lists ingredients where ledger sum ≠ master `current_stock`.
* Action: **Force snapshot** (calls `POST /snapshots/build`) or
  **Manual adjustment** (opens adjustment sheet pre-filled with delta).

---

## 8. Permissions matrix

| Capability                 | Required permission     |
| -------------------------- | ----------------------- |
| View balances / timeline   | `inventory.read`        |
| Create adjustment / wastage| `inventory.update`      |
| Start / finalize a count   | `inventory.update`      |
| Ship / receive transfer    | `inventory.update`      |
| Force snapshot / drift fix | `inventory.admin`       |
| Vendor CRUD                | `inventory.admin`       |

Buttons that require a permission the user lacks **must** be hidden,
not just disabled.

---

## 9. Errors the client must surface gracefully

| HTTP | `code`                       | UX                                                          |
| ---- | ---------------------------- | ----------------------------------------------------------- |
| 409  | `DUPLICATE_IDEMPOTENCY`      | Silent — fetch existing event, reconcile, no toast.         |
| 409  | `INSUFFICIENT_STOCK`         | Toast: "Not enough stock — would go negative". Roll back.   |
| 422  | `RECIPE_MISSING`             | Toast: "No recipe linked". Disable consumption buttons.     |
| 423  | `LEDGER_LOCKED`              | Show non-blocking spinner, retry once after 500 ms.         |
| 5xx  | any                          | Retry banner with manual retry; outbox holds the mutation.  |

---

## 10. Test checklist (Flutter side)

* Balances list survives app cold start with last cached snapshot.
* Wastage created offline appears with "syncing" badge, becomes "synced"
  after wifi returns. Killing app mid-sync still drains correctly on
  next launch.
* Two devices logged into the same branch see the same WS event within
  1 s of a POS order being confirmed.
* A count session with 200 items can be completed with intermittent
  connectivity and zero data loss.
* Permission downgrade (admin → cashier) hides admin-only buttons on
  next screen render without requiring re-login.

---

## 11. Versioning

Contract version: `inventory.v1`. Send header `X-Client-Contract: inventory.v1`
on every request. The server will return `426 Upgrade Required` if the
contract is no longer supported.

Breaking changes always bump the major (`inventory.v2`); additive
changes (new event types, new optional fields) do not.
