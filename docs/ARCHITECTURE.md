# BITTU — System Architecture

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Clients: POS App · KDS Tablet · QR Web · Owner Dashboard · Driver │
└──────────┬──────────────────────────────────────┬───────────────────┘
           │  HTTPS REST                          │  WSS
           ▼                                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      FastAPI  (Uvicorn / Gunicorn)                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│  │ Middleware│→│ Auth/RBAC│→│ Routers  │→│ Services │              │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘              │
│       ↕              ↕              ↕            ↕                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐              │
│  │ Rate Lim │ │State Mach│ │ Events   │ │ Tenant   │              │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘              │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │             WebSocket Manager  ←  Redis Pub/Sub              │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────┬───────────────────┬──────────────────────┬───────────────┘
           ▼                   ▼                      ▼
   ┌──────────────┐   ┌──────────────┐       ┌──────────────┐
   │  PostgreSQL   │   │    Redis     │       │   Razorpay   │
   │  (Supabase)   │   │  Cache/PubSub│       │   Gateway    │
   └──────────────┘   └──────────────┘       └──────────────┘
```

## 2. Directory Structure

```
backend/
├── main.py                      # App factory + lifespan
├── requirements.txt
├── app/
│   ├── __init__.py
│   ├── core/
│   │   ├── config.py            # Pydantic settings (env vars)
│   │   ├── database.py          # asyncpg pool + SQLAlchemy async
│   │   ├── redis.py             # Redis: cache, pub/sub, distributed locks
│   │   ├── logging.py           # structlog JSON logging
│   │   ├── exceptions.py        # Domain exception hierarchy
│   │   ├── state_machines.py    # 6 finite state machines
│   │   ├── events.py            # Domain event bus + Redis pub/sub bridge
│   │   ├── auth.py              # JWT decode, UserContext, RBAC
│   │   └── tenant.py            # Multi-tenant query isolation
│   ├── middleware/
│   │   └── __init__.py          # RequestId, Logging, RateLimit, ErrorHandler
│   ├── services/
│   │   ├── order_service.py     # Order lifecycle
│   │   ├── payment_service.py   # Razorpay integration + refunds
│   │   ├── kitchen_service.py   # KDS station routing
│   │   ├── table_service.py     # QR sessions, cart
│   │   ├── inventory_service.py # Stock deduction / restoration
│   │   ├── delivery_service.py  # Partner assignment, GPS tracking
│   │   ├── staff_service.py     # Branch user CRUD
│   │   ├── subscription_service.py # Razorpay subscriptions
│   │   ├── notification_service.py # In-app alerts
│   │   └── analytics_service.py # Daily aggregation + dashboards
│   ├── realtime/
│   │   └── __init__.py          # WebSocket manager + Redis fan-out
│   └── api/
│       ├── __init__.py          # Central router aggregator
│       └── v1/
│           ├── orders.py
│           ├── payments.py
│           ├── kitchen.py
│           ├── tables.py
│           ├── inventory.py
│           ├── delivery.py
│           ├── staff.py
│           ├── subscriptions.py
│           ├── notifications.py
│           ├── analytics.py
│           ├── webhooks.py
│           └── health.py
```

## 3. Service Breakdown

| Service | Responsibility | Concurrency Strategy | Key Patterns |
|---------|---------------|---------------------|--------------|
| **OrderService** | Full order lifecycle (create → complete/cancel) | Distributed lock per order + SERIALIZABLE tx | Server-side pricing, idempotency key, coupon validation |
| **PaymentService** | Razorpay orders, verification, refunds, audit | Lock per order during payment | Signature verification, webhook idempotency, audit trail |
| **KitchenService** | Route to stations, item-level status tracking | Lock per order during status change | Auto-transition when all items ready |
| **TableSessionService** | QR scan → session → cart → order | Lock per table & per cart | Token-based join, multi-device support |
| **InventoryService** | Ingredient deduction/restoration, PO receiving | SERIALIZABLE + row-level FOR UPDATE | Negative stock prevention, cascading ingredient deduction |
| **DeliveryService** | Partner assignment, GPS tracking, status flow | Lock on delivery + partner | Auto-release on completion, live location via pub/sub |
| **StaffService** | Branch user CRUD, role management | Auth cache invalidation | Owner-only access, cascading cache busts |
| **SubscriptionService** | Razorpay subscription lifecycle, billing | Redis-cached active check | Trial → active → grace → suspended flow |
| **NotificationService** | In-app alerts, read/dismiss | None (low contention) | Event-driven creation |
| **AnalyticsService** | Daily aggregation, dashboard queries | Eventual consistency (scheduled) | Materialized in daily_analytics, 5min cache |

## 4. Data Flow: Order Creation

```
Client POST /api/v1/orders
    │
    ▼
┌──────────────────────────┐
│  Auth middleware          │  Decode JWT → resolve UserContext (cached 5min)
│  Rate limit middleware    │  Sliding window check via Redis
│  Request ID middleware    │  Attach X-Request-ID header
└──────────────┬───────────┘
               ▼
┌──────────────────────────┐
│  OrderService.create()   │
│  1. Check idempotency    │  Redis: has this key been seen?
│  2. Validate subscription│  SubscriptionService.check_active()
│  3. Fetch prices         │  SELECT ... FROM menu_items  ← SERVER-SIDE
│  4. Apply coupon         │  Validate min order, usage limits
│  5. Calculate tax        │  (price × tax_rate) per item
│  6. SERIALIZABLE INSERT  │  orders + order_items in one tx
│  7. Deduct inventory     │  InventoryService.deduct_for_order()
│  8. Set idempotency      │  Mark key done (24h TTL)
│  9. Emit domain event    │  order.created → Redis pub/sub
└──────────────┬───────────┘
               ▼
┌──────────────────────────┐
│  Redis Pub/Sub           │  events:order.created
│      │                   │
│      ▼                   │
│  WS fan-out              │  → branch:{id} channel
│      │                   │  → KDS tablets, POS screens
└──────┴───────────────────┘
```

## 5. Database Transaction Strategy

| Scenario | Isolation Level | Lock Strategy | Rationale |
|----------|----------------|---------------|-----------|
| Order creation | SERIALIZABLE | None (new row) | Prevent phantom reads on coupon usage |
| Order status update | READ COMMITTED | Distributed lock + `FOR UPDATE` | Prevent double-accept race condition |
| Payment verification | READ COMMITTED | Distributed lock per order_id | Prevent double-charge |
| Inventory deduction | SERIALIZABLE | Row-level `FOR UPDATE` on ingredients | Prevent negative stock |
| Table session start | READ COMMITTED | Distributed lock per table_id | Prevent double-booking |
| Delivery assignment | READ COMMITTED | Locks on delivery + partner | Prevent assigning busy partner |

### Why dual locking?

**Distributed lock** (Redis) prevents concurrent *entry* into the critical section across all app instances.  
**Row-level `FOR UPDATE`** (Postgres) is the safety net if Redis lock fails or expires.

## 6. Real-time Architecture

```
                    ┌──────────────────┐
                    │   Service Layer   │
                    │  emit_and_publish │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Redis Pub/Sub    │
                    │  events:*         │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  redis_subscriber │  (background asyncio task)
                    │  Pattern: events:*│
                    └────────┬─────────┘
                             │
                   ┌─────────┴──────────┐
                   │                    │
          ┌────────▼───────┐   ┌───────▼────────┐
          │ branch:{id}    │   │  entity:{id}   │
          │ channel         │   │  channel        │
          └────────┬───────┘   └───────┬────────┘
                   │                    │
            ┌──────▼──────┐     ┌──────▼──────┐
            │ All branch  │     │ Specific    │
            │ staff sockets│     │ order/table │
            └─────────────┘     └─────────────┘
```

**Channel types:**
- `branch:{branch_id}` — all events for a branch (KDS, POS, manager)
- `entity:{order_id}` — a specific order's lifecycle events
- Direct user push — per-user alert delivery

**Client protocol:**
```json
// Subscribe to channels
→ {"action": "subscribe", "channel": "branch:abc-123"}
← {"event": "subscribed", "channel": "branch:abc-123"}

// Receive events
← {"event": "order.created", "data": {...}}

// Heartbeat
← {"event": "ping"}
→ {"action": "pong"}
```

## 7. Failure Handling

| Failure | Mitigation |
|---------|-----------|
| **Redis down** | Rate limiter degrades gracefully (allows all). Auth cache miss → DB lookup. Locks fall through to DB-level FOR UPDATE. |
| **DB connection exhausted** | asyncpg pool with max_size=20. Overflow requests wait with timeout, then fail fast with 503. |
| **Razorpay webhook missed** | Idempotent handlers. Razorpay retries with exponential backoff. Manual reconciliation endpoint planned. |
| **Double-submit** | Idempotency key per order creation (24h TTL). Distributed lock per order status change. |
| **Subscription lapse** | Grace period: 3 failed payment retries → 7 days grace → suspended. Alerts on each failure. |
| **Inventory goes negative** | SERIALIZABLE transaction + explicit stock >= 0 check. Order rejected with InventoryError, all items rolled back. |
| **WebSocket disconnect** | Client reconnects; state is server-side (DB), so no data loss. Future: last_event_id for gap recovery. |

## 8. Security Model

### Authentication
- Supabase Auth issues JWTs (HS256 with project secret)
- Every API request validates JWT in middleware
- User context resolved from `branch_users` or `restaurants` table, cached in Redis

### Authorization (RBAC)
```
Role hierarchy:    owner > manager > chef > waiter > cashier > delivery_partner
```

Each role has specific permissions:
- `owner`: Full access + staff management + analytics
- `manager`: Orders, kitchen, inventory, tables, delivery, analytics
- `chef`: Kitchen display only
- `waiter`: Orders, tables
- `cashier`: Orders, payments
- `delivery_partner`: Assigned deliveries only

### Multi-tenant Isolation
Every query passes through `tenant_where_clause()`:
- **Owner**: scoped to `owner_id = user_id` (across all their branches)
- **Branch user**: scoped to `branch_id = user.branch_id`
- Foreign key relationships prevent cross-tenant data leaks
- No raw user input in SQL — all parameterized queries via asyncpg

### Webhook Security
- Razorpay webhooks validated via HMAC-SHA256 signature
- Idempotent processing (same webhook re-delivery = no-op)

## 9. Scaling Strategy

### Horizontal (Application)
- Stateless FastAPI instances behind a load balancer
- Redis pub/sub ensures WebSocket events reach all instances
- Distributed locks ensure only one instance processes a mutation

### Vertical (Database)
- Supabase managed Postgres with connection pooling (PgBouncer)
- `statement_cache_size=0` for PgBouncer compatibility
- Indexes on all hot query paths (branch_id, status, created_at)
- `daily_analytics` materialized table avoids expensive real-time aggregations

### Caching
- User context: 5 min TTL in Redis
- Analytics dashboard: 5 min TTL
- Subscription status: 10 min TTL
- Menu items for pricing: fetched per order (freshness > speed)

### Future Optimizations
- Read replicas for analytics queries
- Partitioning `orders` by `created_at` (monthly)
- Separate Redis instance for pub/sub vs cache
- Background job queue (Celery/ARQ) for analytics aggregation
- CDN for static assets (menus, images)

## 10. Running the Application

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables (or use .env file)
export SUPABASE_URL=https://your-project.supabase.co
export SUPABASE_KEY=your-anon-key
export SUPABASE_JWT_SECRET=your-jwt-secret
export DATABASE_URL=postgresql://...
export REDIS_URL=redis://localhost:6379/0
export RAZORPAY_KEY_ID=rzp_...
export RAZORPAY_KEY_SECRET=...
export RAZORPAY_WEBHOOK_SECRET=...

# Run
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

# Development
uvicorn main:app --reload
```
