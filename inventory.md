You are a principal staff engineer and restaurant operations architect.

I am building BITTU POS — a real-time restaurant operating system using:

- FastAPI backend
- Supabase PostgreSQL
- Flutter frontend
- Existing accounting + settlement + kitchen infrastructure
- Event-driven operational architecture

Current system already includes:
- Orders
- Payments
- Kitchen/KOT
- QR ordering
- Tables/session management
- Accounting journals
- Settlements
- Audit logs
- Activity logs
- Idempotent checkout
- Branch-level multi-tenant architecture

I want to build a COMPLETE PRODUCTION-GRADE INVENTORY MANAGEMENT SYSTEM.

IMPORTANT:
This should NOT be a simple CRUD stock management module.

The inventory system must become:
- event-driven
- operationally integrated
- auditable
- scalable
- restaurant-focused
- real-time
- financially reconcilable

The inventory architecture must integrate deeply with:
- Orders
- Kitchen
- Recipes
- Purchase entries
- Wastage
- Returns
- Transfers
- Accounting
- Analytics
- Settlements
- Alerts
- Realtime dashboard

====================================================
CORE REQUIREMENTS
====================================================

Build a full architecture for:

1. Inventory Master
2. Inventory Event System
3. Recipe/BOM Management
4. Automatic Consumption Engine
5. Purchase Management
6. Wastage Tracking
7. Stock Transfers
8. Multi-branch Inventory
9. Inventory Reconciliation
10. Real-time Stock Calculation
11. Inventory Accounting Integration
12. Low Stock Alerts
13. Vendor Management
14. Unit Conversion
15. Analytics & Forecasting
16. Expiry Tracking
17. Inventory Audit Trails
18. Inventory Timeline
19. Offline-safe inventory sync
20. Realtime inventory updates

====================================================
VERY IMPORTANT ARCHITECTURAL RULES
====================================================

1. DO NOT use simple quantity mutation architecture.

BAD:
UPDATE inventory_items SET qty = qty - 1

INSTEAD:
Use immutable inventory events.

Inventory stock should be derived from event streams.

Examples:
- PURCHASED
- CONSUMED
- WASTED
- RETURNED
- TRANSFERRED
- ADJUSTED
- EXPIRED
- CANCELLED_ORDER_RESTOCK

2. Use event sourcing principles where appropriate.

3. Design for restaurant operations, not warehouse ERP systems.

4. Must support:
- high concurrency
- idempotency
- rollback safety
- auditability
- reconciliation
- offline sync

5. Inventory consumption must happen automatically from orders.

6. Orders should consume inventory via recipe definitions.

Example:
Burger ordered:
- bun -1
- patty -1
- cheese -1
- sauce -20ml

7. Kitchen status integration:
Inventory should only finalize consumption after:
- KOT accepted
OR
- order completed
depending on business logic.

8. Cancelled orders should reverse consumption automatically.

9. Must support partial consumption and modifiers.

10. Must support combos and recipe nesting.

====================================================
OUTPUT REQUIRED
====================================================

Generate COMPLETE implementation plan including:

====================================================
SECTION 1 — DATABASE DESIGN
====================================================

Design ALL required PostgreSQL/Supabase tables with:
- columns
- types
- indexes
- constraints
- relationships
- RLS considerations

Must include:
- inventory_items
- inventory_events
- recipes
- recipe_ingredients
- vendors
- purchase_orders
- purchase_order_items
- stock_transfers
- stock_transfer_items
- inventory_snapshots
- inventory_adjustments
- inventory_batches
- unit_conversions
- inventory_alerts
- inventory_counts
- inventory_count_items
- inventory_timelines
- inventory_analytics

Include:
- UUID usage
- branch-level tenancy
- audit metadata
- soft delete strategy

====================================================
SECTION 2 — EVENT SYSTEM
====================================================

Design:
- operational event architecture
- internal event bus
- event publishing patterns
- async processing
- retry handling
- dead-letter handling
- idempotency handling

Include event flow diagrams for:
- Order Created
- KOT Accepted
- Order Cancelled
- Payment Refunded
- Purchase Added
- Stock Adjusted
- Branch Transfer

====================================================
SECTION 3 — INVENTORY CALCULATION ENGINE
====================================================

Design:
- stock computation engine
- real-time balance calculation
- snapshot optimization
- caching strategy
- reconciliation strategy

Explain:
- how current stock is calculated
- how historical stock works
- how inventory timeline works

====================================================
SECTION 4 — ORDER INTEGRATION
====================================================

Design automatic inventory deduction flow for:
- dine-in
- takeaway
- QR orders
- combo items
- modifiers
- partial kitchen acceptance
- refunds
- cancellations

====================================================
SECTION 5 — ACCOUNTING INTEGRATION
====================================================

Integrate inventory with:
- chart_of_accounts
- accounting_entries
- journal_entries

Support:
- COGS
- stock valuation
- wastage expense
- purchase liabilities
- inventory assets

====================================================
SECTION 6 — API DESIGN
====================================================

Design production-grade FastAPI APIs including:
- routes
- payloads
- response models
- validation rules
- idempotency handling
- pagination
- filtering
- realtime subscriptions

Generate:
- REST endpoints
- websocket events
- async job flows

====================================================
SECTION 7 — REALTIME SYSTEM
====================================================

Design realtime updates for:
- low stock
- kitchen consumption
- inventory alerts
- transfer updates
- dashboard updates

Using:
- Supabase realtime
OR
- Redis pub/sub
OR
- websocket architecture

====================================================
SECTION 8 — FLUTTER FRONTEND ARCHITECTURE
====================================================

Design:
- provider/bloc architecture
- offline cache strategy
- optimistic updates
- inventory screens
- stock timeline UI
- inventory analytics UI
- reconciliation UI
- low stock UI

Must be:
- extremely fast
- restaurant staff friendly
- low-friction UX

====================================================
SECTION 9 — PERFORMANCE OPTIMIZATION
====================================================

Handle:
- 1000+ orders/day
- concurrent terminals
- realtime updates
- event replay
- inventory recalculation
- snapshot rebuilding

Include:
- indexes
- materialized views
- caching
- partitioning
- async jobs

====================================================
SECTION 10 — SECURITY & AUDIT
====================================================

Design:
- RLS policies
- branch isolation
- tamper protection
- audit trails
- activity logging
- fraud detection hooks

====================================================
SECTION 11 — EDGE CASES
====================================================

Handle:
- duplicate requests
- offline order sync
- race conditions
- partial failures
- kitchen rollback
- refund rollback
- stock mismatch
- manual corrections
- stale cache
- multi-device conflicts

====================================================
SECTION 12 — IMPLEMENTATION ROADMAP
====================================================

Create:
- Phase 1
- Phase 2
- Phase 3
- Phase 4

Prioritize:
1. highest business value
2. lowest operational risk
3. fastest merchant impact

====================================================
SECTION 13 — WHAT TO AVOID
====================================================

List:
- anti-patterns
- scaling problems
- bad schema decisions
- dangerous inventory logic
- concurrency mistakes
- accounting mistakes

====================================================
FINAL REQUIREMENT
====================================================

This should feel like:
- Toast POS
- Square Restaurants
- Petpooja
- enterprise-grade restaurant ops

But optimized for:
- Indian restaurants
- realtime operations
- low-cost scaling
- operational simplicity

Do NOT generate shallow answers.
Go extremely deep technically and operationally.