# Bittu Backend — Architecture v2 Blueprint

> **Status:** Approved blueprint. Migration is incremental and non-breaking; v1 stays live until each domain cuts over.
> **Owner:** Platform / Backend
> **Audience:** Backend, Platform Ops, Frontend, SRE

---

## 0. North Star

Evolve a feature-grouped FastAPI monolith into a **domain-driven, fintech-grade modular monolith** with:

- Six top-level API domains (`platform / merchant / branch / public / internal / financial`)
- A separate **Financial Infrastructure** domain that owns *all* money movement
- Banking-grade invariants: append-only ledger, double-entry, replay-safe, reconciliation-safe
- Multi-tenant isolation at the database (RLS) **and** dependency layer
- Future microservice extraction along domain seams — without doing it today

**What it is NOT:**
- Not a microservices migration
- Not a rewrite — code stays put; **ownership and routing change**
- Not a frontend break — v1 paths remain live during migration

---

## 1. Target API Domain Surface

| Domain | Prefix | Audience | Auth | Tenancy |
|---|---|---|---|---|
| Platform | `/api/platform/v1` | Bittu staff | Platform JWT + role | global |
| Merchant | `/api/merchant/v1` | Restaurant owners/admins | Merchant JWT | tenant-scoped |
| Branch | `/api/branch/v1` | Cashiers, kitchen, waiters | Merchant JWT + branch claim | tenant + branch-scoped |
| Public | `/api/public/v1` | Diners, webhooks, callbacks | none / signed token | none |
| Internal | `/api/internal/v1` | Workers, schedulers, M2M | service token (HMAC + IP allowlist) | global |
| Financial | `/api/financial/v1` | Internal callers + platform | Service token (write) / Platform JWT (read) | tenant in payload |

> **Why split Financial from Platform?** Financial is the *engine*. Platform is the *operator UI*. Splitting lets us extract Financial as the first microservice when load demands it, without rewriting Platform.

---

## 2. Folder Layout (Target)

```
app/
├── api/                             # HTTP transport only — thin
│   ├── platform/                    # /api/platform/v1/*
│   │   ├── v1/
│   │   │   ├── merchants.py
│   │   │   ├── kyc.py
│   │   │   ├── fee_plans.py
│   │   │   ├── payouts.py
│   │   │   ├── escrow.py
│   │   │   ├── reconciliation.py
│   │   │   ├── disputes.py
│   │   │   ├── refunds.py
│   │   │   ├── audit.py
│   │   │   ├── risk.py
│   │   │   ├── fin_reports.py
│   │   │   └── support.py
│   │   └── __init__.py              # aggregates v1 routers
│   ├── merchant/v1/
│   │   ├── operations/              # orders, menu, items, kitchen, dinein, customers,
│   │   │                            # inventory, purchase_orders, staff, notifications,
│   │   │                            # restaurant_settings
│   │   └── financial/               # payments, settlements, refunds, invoices, statements,
│   │                                # accounting, expenses, ledger, reports, analytics
│   ├── branch/v1/                   # pos, orders, kitchen, tables, qr, waiter, cashier
│   ├── public/v1/                   # qr_menu, qr_order, public_invoice, payment_callback
│   ├── internal/v1/                 # recon_worker, settlement_worker, webhook_processor,
│   │                                # snapshot_worker, risk_worker, analytics_worker
│   ├── financial/v1/                # ledger, escrow, journal, accounting, recon, settlement,
│   │                                # payout, dispute, fee, events
│   └── __init__.py                  # mounts all six domain routers
│
├── domains/                         # ← NEW: domain-driven business logic
│   ├── identity/                    # auth, users, roles, sessions, JWT
│   │   ├── repositories/
│   │   ├── services/
│   │   ├── models/
│   │   └── events.py
│   ├── tenancy/                     # merchants, branches, memberships
│   ├── catalog/                     # menu, items, categories, modifiers, combos
│   ├── ordering/                    # orders, dine-in sessions, kitchen tickets
│   ├── inventory/                   # stock, ingredients, snapshots, deductions
│   ├── customers/                   # crm, addresses, favourites, loyalty
│   ├── payments/                    # gateway adapters, payment intents, captures
│   ├── financial/                   # ← Financial Infrastructure (see §6)
│   │   ├── ledger/                  # merchant_ledger, escrow_ledger
│   │   ├── journal/                 # double-entry journal engine
│   │   ├── accounting/              # COA, periods, postings, statements
│   │   ├── reconciliation/          # bank_recon, cross_table_recon, recon_engine
│   │   ├── settlement/              # settlement orchestration
│   │   ├── payout/                  # payout orchestration
│   │   ├── refunds/
│   │   ├── disputes/
│   │   ├── fees/                    # fee plans, fee computations
│   │   └── events/                  # financial_events store
│   ├── erp/                         # purchase orders, expenses, invoices
│   ├── compliance/                  # kyc, risk, audit
│   ├── notifications/
│   └── shared/                      # cross-domain value objects, exceptions
│
├── core/                            # framework primitives (no business logic)
│   ├── config.py
│   ├── database.py                  # connection pools (R/W split-ready)
│   ├── redis.py
│   ├── logging.py
│   ├── metrics.py
│   ├── tracing.py                   # ← NEW: OTel setup
│   ├── correlation.py               # ← NEW: request-id propagation
│   ├── auth/
│   │   ├── jwt.py
│   │   ├── claims.py                # ← NEW: scoped JWT claim model
│   │   ├── service_token.py         # ← NEW: HMAC-signed M2M tokens
│   │   ├── permissions.py           # ← role → permission matrix
│   │   └── rbac.py                  # ← role hierarchy enforcement
│   ├── audit/                       # immutable audit emitter (used by all domains)
│   ├── events/                      # event bus (in-process today, queue-ready)
│   ├── outbox/                      # ← NEW: transactional outbox helpers
│   ├── exceptions.py
│   ├── state_machines.py
│   └── webhook_security.py
│
├── dependencies/
│   ├── auth.py                      # require_user, require_platform_user, etc.
│   ├── scopes.py                    # ← NEW: require_platform_scope("payouts:write")
│   ├── tenancy.py                   # ← NEW: require_tenant, require_branch
│   ├── service.py                   # ← NEW: require_service_token
│   └── rbac.py                      # legacy, kept until migration done
│
├── middleware/
│   ├── request_id.py
│   ├── logging.py
│   ├── error_handler.py
│   ├── security_headers.py
│   ├── rate_limit.py
│   ├── tenant_isolation.py          # ← NEW: forbids cross-tenant data leak
│   └── audit_trail.py               # ← NEW: writes to audit_events
│
├── workers/                         # ← NEW: background job handlers
│   ├── reconciliation/
│   ├── settlement/
│   ├── payout/
│   ├── snapshot/
│   ├── webhook_processor/
│   └── outbox_dispatcher/           # transactional outbox publisher
│
├── realtime/                        # WS / SSE
├── schemas/                         # pydantic — kept, but split by domain
└── templates/
```

**Why this shape?**
- `api/` is a **thin transport layer**: parses, validates, calls domain service, formats response. No business logic.
- `domains/` is the **business core**: independent of FastAPI, repositories per aggregate root, services per use case. Future microservice cut lines run between domain folders.
- `workers/` is **separable**: when load demands, point a separate process at this package and disable the corresponding ASGI mount.

---

## 3. Router Hierarchy

### 3.1 Mount points

```python
# app/api/__init__.py
api_router = APIRouter()
api_router.include_router(platform_router, prefix="/platform/v1", tags=["platform"])
api_router.include_router(merchant_router, prefix="/merchant/v1", tags=["merchant"])
api_router.include_router(branch_router,   prefix="/branch/v1",   tags=["branch"])
api_router.include_router(public_router,   prefix="/public/v1",   tags=["public"])
api_router.include_router(internal_router, prefix="/internal/v1", tags=["internal"])
api_router.include_router(financial_router,prefix="/financial/v1",tags=["financial"])

# Legacy — kept live with deprecation header during cutover
api_router.include_router(legacy_v1_router, prefix="/v1", tags=["legacy"])
```

### 3.2 Domain aggregator pattern

```python
# app/api/platform/__init__.py
from fastapi import APIRouter
from app.api.platform.v1 import (
    merchants, kyc, fee_plans, payouts, escrow, reconciliation,
    disputes, refunds, audit, risk, fin_reports, support,
)

router = APIRouter(dependencies=[Depends(require_platform_user)])
for m in (merchants, kyc, fee_plans, payouts, escrow, reconciliation,
          disputes, refunds, audit, risk, fin_reports, support):
    router.include_router(m.router)
```

> **Why per-domain `dependencies=[]`?** Tenant/role enforcement lives at the *router boundary*, not in every handler. Impossible to forget.

---

## 4. Service & Repository Architecture

### 4.1 Layered with strict direction

```
api (FastAPI)
  └─→ application services (use cases, orchestrate domain)
        └─→ domain services (pure business rules)
              └─→ repositories (data access, one per aggregate)
                    └─→ database / external adapters
```

- **Repositories** own SQL. Services NEVER write SQL inline.
- **Domain services** know nothing about FastAPI, requests, or HTTP.
- **Application services** are the use-case layer that the API calls.

### 4.2 Why this matters
- **Testability** — domain services unit-tested without DB.
- **Microservice extraction** — application + domain layer is portable, only repositories swap.
- **Reconciliation safety** — financial writes go through `JournalRepository` which enforces double-entry; impossible to bypass.

### 4.3 Repository contract example

```python
class MerchantLedgerRepository(Protocol):
    async def append(self, entry: LedgerEntry, *, conn: Connection) -> LedgerEntryId: ...
    async def get_balance(self, merchant_id: MerchantId, *, as_of: datetime) -> Money: ...
    async def list_entries(self, merchant_id, *, page, filters) -> Page[LedgerEntry]: ...
```

> Repositories accept an explicit `conn` to compose into the *caller's* transaction. This is non-negotiable for financial integrity.

---

## 5. Dependency Injection Structure

### 5.1 Six required DI primitives

| Dep | Purpose | Used by |
|---|---|---|
| `require_user` | any authenticated user | merchant + branch |
| `require_platform_user` | platform staff JWT | platform |
| `require_service_token` | HMAC service-to-service | internal + financial |
| `require_tenant(level="merchant"\|"branch")` | injects + enforces tenant scope | merchant + branch |
| `require_scope("payouts:write")` | RBAC scope gate | every write endpoint |
| `idempotency_key(required=True)` | required for money writes | financial + payment writes |

### 5.2 Composition

```python
WriteMoney = [
    Depends(require_service_token),
    Depends(require_scope("ledger:write")),
    Depends(idempotency_key(required=True)),
]

@router.post("/post", dependencies=WriteMoney)
async def post_journal(...): ...
```

> **Why scopes and not roles in handlers?** Roles live in JWT claims; **scopes live on endpoints**. Adding a new role doesn't require touching a single endpoint — only the role→scope matrix.

---

## 6. Financial Domain Separation

### 6.1 Hard rules
1. **All money movement** goes through `domains/financial/journal/JournalEngine.post(entries)`.
2. **No service** outside `domains/financial/` may run `INSERT/UPDATE/DELETE` on `journal_lines`, `*_ledger`, `escrow_*`, `payouts*`, `settlements*`, `refunds*`, `disputes*`.
3. **Append-only**. Corrections are *reversal entries*, never UPDATEs.
4. **Double-entry enforced at write time**: `sum(debits) == sum(credits)` per posting, asserted in DB constraint and in `JournalEngine`.
5. **Idempotency required**: every money write requires `Idempotency-Key`; first write recorded in `idempotency_keys`, subsequent identical requests return cached response.
6. **Transactional outbox**: financial events written in same transaction as the journal posting; `outbox_dispatcher` worker publishes downstream.

### 6.2 Engine boundaries

```
domains/financial/
  journal/              JournalEngine.post() — only writer of journal_lines
  ledger/               read-side projection of journal_lines, append cache
  accounting/           COA, period close, statements (read-side)
  reconciliation/       matches external feeds → journal entries
  settlement/           T+N settlement orchestration → journal entries
  payout/               payout requests → settlement → bank transfer
  refunds/              refund orchestration → reverse journal entries
  disputes/             chargeback workflow + reserve postings
  fees/                 fee computation (pure function, no DB writes)
  events/               financial_events store (immutable)
```

### 6.3 Why immutable + double-entry?
- **Audit defensibility** in any financial dispute.
- **Reconciliation correctness**: replay the journal → must reproduce balance.
- **Regulatory readiness** for RBI/PA-PG audits.

---

## 7. RBAC Architecture

### 7.1 Three-layer model

1. **Role** — a JWT claim. Stable, coarse.
2. **Scope** — what an endpoint requires (e.g., `payouts:write`).
3. **Tenant binding** — which `merchant_id`/`branch_id` the user is authorized for.

### 7.2 Roles per layer

| Layer | Roles |
|---|---|
| Platform | `super_admin`, `finance_admin`, `recon_admin`, `risk_admin`, `support_admin` |
| Merchant | `merchant_owner`, `merchant_admin`, `branch_manager` |
| Branch | `cashier`, `waiter`, `kitchen_staff`, `inventory_staff` |
| Internal | `recon_worker`, `payout_worker`, `risk_worker`, `settlement_worker`, `webhook_worker` |

### 7.3 Mapping table (excerpt)

| Scope | super_admin | finance_admin | merchant_owner | branch_manager | cashier |
|---|---|---|---|---|---|
| `merchants:write` | ✓ | – | – | – | – |
| `payouts:approve` | ✓ | ✓ | – | – | – |
| `ledger:read` | ✓ | ✓ | own merchant | own branch | – |
| `orders:write` | – | – | own merchant | own branch | own branch |
| `kitchen:read` | – | – | own merchant | own branch | – |

> **Why scope/role split?** Stripe's "permissions are per-endpoint, roles are per-user" model. Single source of truth, easy to extend, easy to audit.

---

## 8. Middleware Architecture

Order is **load-bearing**:

```
1. SecurityHeadersMiddleware     (always last response touch)
2. ErrorHandlerMiddleware         (catches everything below)
3. RequestIdMiddleware            (correlation id header propagation)
4. TracingMiddleware              (OTel span per request)
5. RequestLoggingMiddleware       (structured access log w/ correlation id)
6. RateLimitMiddleware            (per-tenant token bucket)
7. TenantIsolationMiddleware      (sets RLS GUC, rejects mismatched claims)
8. AuditTrailMiddleware           (write to audit_events on success of write methods)
```

> **Why TenantIsolationMiddleware?** Sets `SET LOCAL app.current_tenant = <merchant_id>` per request. RLS policies use this. Even a developer mistake in a query cannot leak across tenants.

---

## 9. Internal Worker Architecture

### 9.1 Pattern: Outbox + Worker

```
domain service writes:
  ├─ business table (e.g., orders)
  └─ outbox table (event row) ────┐
                                  │ same transaction
                                  ▼
              outbox_dispatcher (worker, polls every 1s)
                                  │
                                  ▼
                      Redis Stream / SQS topic
                                  │
                                  ▼
                  recon_worker / settlement_worker / ...
```

### 9.2 Worker types

| Worker | Trigger | Idempotency | Replayable |
|---|---|---|---|
| `outbox_dispatcher` | poll | event_id dedup | yes |
| `recon_worker` | event / cron | external_ref dedup | yes |
| `settlement_worker` | cron (T+1) | settlement_batch_id | yes |
| `payout_worker` | event + manual approve | payout_id | yes |
| `webhook_processor` | gateway webhook | webhook_id | yes |
| `snapshot_worker` | cron (hourly) | snapshot_at | yes |
| `risk_worker` | event | rule_run_id | yes |

> **Why outbox?** Eliminates dual-write inconsistency. The event is *guaranteed* to fire iff the business write committed.

---

## 10. API Versioning Strategy

- **Domain-prefixed versioning**: `/api/<domain>/v1/...`
- Each domain versions independently. Merchant can be on `v2` while Branch stays on `v1`.
- Breaking change rules:
  - **Patch (silent):** new optional response fields
  - **Minor:** new endpoints, new optional request fields
  - **Major:** new `vN` mount; old `v(N-1)` lives 6 months with `Deprecation` header
- Legacy `/api/v1/*` continues to mount existing routers via a compatibility shim until callers migrate.

---

## 11. OpenAPI Grouping

Six **separate OpenAPI documents**, one per domain:

```
GET /api/platform/v1/openapi.json
GET /api/merchant/v1/openapi.json
GET /api/branch/v1/openapi.json
GET /api/public/v1/openapi.json
GET /api/internal/v1/openapi.json
GET /api/financial/v1/openapi.json
```

- Built by mounting each domain as a sub-app: `app.mount("/api/platform/v1", platform_app)`.
- Public docs (`/docs/platform`, `/docs/merchant`, ...) served conditionally based on caller identity.
- Internal + financial OpenAPI is *not* exposed to merchant frontends.

> **Why separate?** A merchant frontend should not be able to discover platform endpoints. Postman + frontend codegen become per-domain.

---

## 12. Event-Driven Architecture

### 12.1 Event taxonomy

```
{domain}.{aggregate}.{verb_past_tense}     e.g.  ordering.order.placed
                                                  financial.payout.approved
                                                  financial.journal.posted
```

### 12.2 Bus

- **Today:** in-process pub/sub (`app/core/events.py`) + outbox table
- **Tomorrow:** `outbox_dispatcher` publishes to Redis Streams (already deployed) → consumer groups per worker
- **Day after:** swap Redis Streams for Kafka without touching domain code (only the dispatcher target)

### 12.3 Event store

`financial_events` table is **the** source of truth for any financial state. All read-side projections (ledger balance, statement, dashboard) are derived. This is event-sourcing for the financial domain only — pragmatic, not religious.

---

## 13. Reconciliation Domain Architecture

```
Bank statement ─┐
Gateway report ─┼─→ ingestion adapters → raw_external_txns (immutable)
Internal txns  ─┘                          │
                                           ▼
                                   match_engine (deterministic + heuristic)
                                           │
                       ┌───────────────────┼───────────────────┐
                       ▼                   ▼                   ▼
                  matched_pairs      unmatched_internal   unmatched_external
                       │
                       ▼
              JournalEngine.post(reconciled entries)
                       │
                       ▼
               financial_events.recon_completed
```

- **Reconciliation is read-mostly + posts journal entries**. Never mutates source data.
- Match strategies are pluggable (`exact_amount_date`, `fuzzy_amount`, `reference_number`).
- Manual override produces a journal entry with operator attribution.

---

## 14. Escrow Domain Architecture

- Escrow = **liability** held against future payout obligations.
- Every gateway-collected payment posts:
  ```
  Dr  Bank-Receivable
  Cr  Escrow-Liability (held T+N days per fee_plan)
  ```
- Settlement worker, on T+N: 
  ```
  Dr  Escrow-Liability
  Cr  Merchant-Wallet (less fees)
  Cr  Platform-Revenue (fees)
  ```
- Reserve postings (for chargebacks/disputes) are sub-accounts of escrow with their own release schedule.

> **Why this matters:** Escrow balance must equal `sum(unsettled payments) - sum(settled payouts) - sum(reserves)` at all times. Enforced by reconciliation worker daily.

---

## 15. Merchant Financial Dashboard Architecture

Read model only. Source: projections built from `financial_events` + `journal_lines`.

| Card | Source query | Refresh |
|---|---|---|
| Cash income today | `journal_lines WHERE account=cash_in AND date=today` | live |
| Online income today | `journal_lines WHERE account=online_in AND date=today` | live |
| UPI / Card / etc. breakdown | `payments JOIN journal_lines` by `gateway` | live |
| Pending settlement | `escrow_balance(merchant)` | live |
| Last payout | `payouts WHERE merchant ORDER BY paid_at DESC LIMIT 1` | live |
| Fees deducted (MTD) | `journal_lines WHERE account=platform_fee` | hourly mview |
| GST liability (MTD) | `tax_invoices` aggregation | daily |
| Branch-wise sales | order facts by branch | hourly mview |
| P&L | `accounting/period_pnl(merchant, period)` | daily |
| Growth analytics | order facts time-series | hourly mview |

All exposed under `/api/merchant/v1/financial/dashboard`. **No direct table reads** — every card is a service method.

---

## 16. Platform Operations Architecture

`/api/platform/*` provides:

| Capability | Endpoint group | Why |
|---|---|---|
| Merchant lifecycle | `/merchants` | activate, suspend, force-logout |
| KYC review queue | `/kyc` | approve / reject with reason + audit |
| Fee plan editor | `/fee-plans` | versioned, never edited in place |
| Payout console | `/payouts` | review, approve, hold, retry |
| Escrow inspector | `/escrow` | per-merchant liability, release schedule |
| Recon dashboard | `/reconciliation` | unmatched count, open since, drilldown |
| Disputes desk | `/disputes` | chargeback queue, evidence upload |
| Refund desk | `/refunds` | manual refund, refund analytics |
| Risk console | `/risk` | velocity rules, blocked entities |
| Audit explorer | `/audit` | full audit_events search |
| Financial reports | `/fin-reports` | platform P&L, take-rate, MRR |

Every write here emits `audit_events.platform_action` with operator id, IP, request id, before/after diff.

---

## 17. Database Domain Boundaries

Single Postgres instance, **but** logical schemas to mark domain ownership and enable future split:

```sql
-- target schemas (introduced in a future migration)
CREATE SCHEMA identity;
CREATE SCHEMA tenancy;
CREATE SCHEMA catalog;
CREATE SCHEMA ordering;
CREATE SCHEMA inventory;
CREATE SCHEMA customers;
CREATE SCHEMA payments;
CREATE SCHEMA financial;     -- ledger, journal, escrow, settlements, payouts, refunds, disputes, fees
CREATE SCHEMA erp;           -- POs, invoices, expenses
CREATE SCHEMA compliance;    -- kyc, audit, risk
```

Rules:
- Cross-schema FK only **into** `tenancy` (everything references `tenancy.merchants`).
- A domain may **read** another domain's tables only via a `*_view` published by that domain.
- Writes never cross schemas. Cross-domain effects flow through events.

> **Why not separate DBs today?** Distributed transactions are a different problem. Schemas give 80% of the boundary benefit with 0% of the operational cost.

---

## 18. Queue + Worker Design

### Today (v2.0)
- Redis Streams as transport (already deployed)
- Workers run inside the same gunicorn process pool (cron + outbox dispatcher)
- Idempotency via `idempotency_keys` table

### Tomorrow (v2.1)
- Workers split into a separate systemd unit: `bittu-workers.service`
- Same code path; only the entrypoint changes
- ASGI process stops mounting worker schedulers

### Day after (v2.2)
- Replace Redis Streams with Kafka or SQS
- Only `outbox_dispatcher` target changes; consumers untouched

---

## 19. Security Model

| Concern | Mechanism |
|---|---|
| Tenant isolation | `TenantIsolationMiddleware` sets RLS GUC + RLS policies on every public table |
| Branch isolation | Scopes resolve to branch-bound queries; repos take branch_id |
| Role enforcement | `require_scope(...)` per endpoint |
| Service tokens | HMAC-SHA256 with rotating secrets, IP allowlist for `/internal/*` |
| Replay safety | `Idempotency-Key` required for money writes, 24h TTL |
| Webhook auth | per-gateway signature verification in `webhook_security.py` |
| JWT claims | `{ sub, role, merchant_id, branch_id?, scopes[] }` — scopes derived server-side |
| Audit | every write to `audit_events` with correlation id |
| Secrets | env via `pydantic-settings`, no secrets in code or logs |
| RLS | deny-all default (already shipped in migration 056) |

---

## 20. Production Deployment Architecture

### 20.1 Topology (target)

```
                                    ┌── Gunicorn(API) ── 4× uvicorn workers
ALB ── nginx ── upstream pools ─────┤
                                    └── Gunicorn(workers) ── N× workers (separate unit)

                                    ┌── Postgres (Supabase) — primary
                                    ├── Read replica (analytics + dashboards)
                                    ├── Redis (cache + streams)
                                    └── S3 (statements, KYC docs, receipts)
```

### 20.2 systemd units

- `bittu.service` — API process pool
- `bittu-workers.service` — workers (new, deployed in Phase 5 of migration)
- `bittu-cron.service` — schedulers (new, deployed in Phase 5)

### 20.3 Observability

- **Metrics:** Prometheus scrape `/metrics` on each unit; per-domain labels
- **Tracing:** OTel SDK → OTLP → (Tempo / Jaeger / Honeycomb)
- **Logs:** structured JSON with `correlation_id`, `tenant_id`, `domain`, `scope`
- **Financial lineage:** every journal entry carries `correlation_id`; given any DB row you can trace back to the originating request

---

## 21. Migration Phases (HOW we get there without breaking prod)

| Phase | Scope | Risk | Cutover |
|---|---|---|---|
| **0 — Skeleton** *(this commit)* | Create empty domain routers + folders. Nothing routes through them yet. Legacy `/api/v1/*` untouched. | None | n/a |
| **1 — Auth & RBAC** | Add `core/auth/claims.py`, `dependencies/scopes.py`, role→scope matrix. Existing endpoints unchanged. | Low | n/a |
| **2 — Mount domains** | Mount each new domain at its prefix; *re-export* existing v1 handlers behind it. Both URLs work. Frontend can start migrating per endpoint. | Low | dual-mount |
| **3 — Move handlers physically** | Move file-by-file from `app/api/v1/*.py` into `app/api/<domain>/v1/*.py`. Update tests. v1 shim re-imports the new locations. | Medium | reversible per file |
| **4 — Extract domains** | Move services into `app/domains/<domain>/services/`. Introduce repositories. | Medium | per domain |
| **5 — Workers split** | Move workers to `app/workers/`, deploy `bittu-workers.service`. Stop scheduling them in API process. | Medium | feature-flagged |
| **6 — Schemas + RLS tightening** | Move tables into per-domain schemas via online DDL. Update RLS policies. | High | one domain at a time |
| **7 — Retire legacy v1** | Remove `/api/v1/*` mount once all callers migrated. | Low (after #2 sunset) | flag-controlled |

> **Discipline:** every phase is a separate PR. Every phase passes the smoke tests we already have. Every phase is reversible by `git revert`.

---

## 22. Why Each Choice Matters (cheat sheet)

| Choice | Why | Scalability | Fintech | Recon | Ops |
|---|---|---|---|---|---|
| Six domain split | Clear ownership | Can extract any one as service | Financial isolated | Recon owns its tables | On-call paging by domain |
| Modular monolith | One DB transaction across domains | Scales to ~10× current load | Cross-domain ACID preserved | Single-tx posts | One deploy unit |
| Append-only ledger | Cannot lose history | Index-friendly | Audit-defensible | Replay = truth | Bug fixes via reversal entries |
| Outbox + workers | No dual-write loss | Workers scale independently | Events guaranteed | Worker retries safe | Workers restartable |
| RLS deny-all | Defense in depth | Free at scale | Multi-tenant safe | Recon respects tenants | Bug-tolerant |
| Scoped JWT + endpoint scopes | Single source of truth | No fan-out auth checks | Operator attribution | Recon ops attributable | New roles = config |
| Service tokens for /internal | Frontend cannot reach workers | Stateless | M2M attestation | Worker ID in audit | IP allowlist + rotate |
| Per-domain OpenAPI | Smaller blast radius | Frontend codegen split | Internal not exposed | – | Postman per audience |
| Schema-per-domain | Future split-DB ready | Plan for read-replicas | Financial schema = first split candidate | Recon stays with financial | DBA boundaries clear |
| Idempotency required for money writes | No duplicate postings | High-concurrency safe | Bank-grade | Recon stable | Retry-safe clients |
| Correlation id propagation | Trace any request end-to-end | – | Audit lineage complete | Recon op traceable | One-click drill-down |

---

## 23. Non-Goals

- Microservices today.
- Rewriting handlers from scratch.
- Changing the public v1 API surface during phases 0–2.
- Touching `migrations/*.sql` history. New work goes into new migrations only.
- Replacing the existing payment gateway adapters or reimplementing payments.
- Switching ORM. Stay with raw SQL via repositories.

---

## 24. Definition of Done (per phase)

For every phase:
1. ✅ All tests green (`pytest -q`).
2. ✅ Compileall clean.
3. ✅ Boot test: `python -c "from main import app"` succeeds, route count ≥ previous.
4. ✅ Smoke a representative endpoint per domain.
5. ✅ Deployed to EC2; `systemctl is-active bittu.service` returns `active`.
6. ✅ Journalctl tail clean for 60s after restart.
7. ✅ Doc updated (`docs/ARCHITECTURE_V2.md` Phase status table).

---

## 25. Open Questions

1. **OTel destination.** Tempo on EC2 vs. SaaS (Honeycomb/Datadog). Decision needed before Phase 5.
2. **Read replica.** When do we cut the analytics dashboards over to a replica? Recommend after Phase 4.
3. **Worker host.** Same EC2 instance with separate systemd unit, or new instance? Cheap path: same instance until CPU > 60%.
4. **API gateway.** Drop nginx in favor of ALB-only? Defer until Phase 7.

---
*This document is the source of truth. Update it as phases land.*
