# Frontend Prompt — Razorpay Settlements (React)

> **Goal:** Build a "Settlements" screen where an **account holder (merchant/owner)**
> can view their **Razorpay settlement status** — i.e. when Razorpay paid money
> into their bank account, how much, the bank UTR reference, and the per-payment
> breakdown of each payout. All data is read from **our backend**, which mirrors
> Razorpay's settlement state locally and is auto-refreshed by a background
> poller. The frontend never talks to Razorpay directly.

---

## Context

A "settlement" is a payout Razorpay makes to the merchant's bank account,
batching many captured payments (minus fees + GST) into a single bank transfer.
Each settlement row carries a **UTR** (the bank reference the merchant can match
against their bank statement) and a **status** in its lifecycle.

The settlements are **already scoped to the logged-in account holder** by the
backend using the JWT — the frontend does **not** send any `restaurant_id`,
`merchant_id`, or `account_id`. The merchant only ever sees their own
settlements.

---

## Conventions

- Base URL: all routes are prefixed with `/api/v1`.
- Auth: every request needs `Authorization: Bearer <jwt>`.
- Permission required to read: `razorpay.settlements.read` (owners/managers have
  it). If the user lacks it the API returns `403` — render an empty/locked state.
- **All money is in paise (integer).** Divide by 100 for rupees. Never do float
  math on rupees — format from paise: `(amount_paise / 100).toLocaleString('en-IN', { style: 'currency', currency: 'INR' })`.
- Timestamps are ISO-8601 UTC strings (or `null`). Render in the merchant's
  local timezone.
- Dates like `created_for_date` are `YYYY-MM-DD` (the business day the payout
  covers).

---

## Settlement status values

`status` is one of:

| status        | meaning                                              | UI treatment          |
| ------------- | ---------------------------------------------------- | --------------------- |
| `pending`     | Settlement created, not yet sent to bank             | grey / "Scheduled"    |
| `processing`  | Razorpay is transferring to the bank                 | blue / "In progress"  |
| `processed`   | Money credited to the merchant's bank (UTR present)  | green / "Settled"     |
| `failed`      | Payout failed                                        | red / "Failed"        |
| `reversed`    | Settlement reversed                                  | amber / "Reversed"    |

Only show the `utr` chip (copy-to-clipboard) when `status === 'processed'` and
`utr` is non-null.

---

## Endpoints

### 1. List settlements — `GET /api/v1/razorpay-settlements`

The main list for the screen.

Query params (all optional):

| param      | type            | notes                                            |
| ---------- | --------------- | ------------------------------------------------ |
| `status`   | string          | one of the status values above                   |
| `from_ts`  | ISO datetime    | only settlements created on/after this instant   |
| `to_ts`    | ISO datetime    | only settlements created strictly before this    |
| `limit`    | int 1..200      | default `50`                                     |
| `offset`   | int ≥ 0         | default `0` — for pagination                      |

Response `200` — array (newest first by `created_at`):

```json
[
  {
    "id": "12",
    "settlement_id": "setl_PabC123XyZ",
    "merchant_id": "751c6d1d-1559-45f2-a24b-7ecd16678113",
    "linked_account_id": "acc_PqR456",
    "amount_paise": 1899850,
    "fees_paise": 4500,
    "tax_paise": 810,
    "utr": "CITIN22045678901",
    "status": "processed",
    "settled_at": "2026-06-05T11:32:04Z",
    "created_for_date": "2026-06-04",
    "created_at": "2026-06-05T05:30:00Z",
    "updated_at": "2026-06-05T11:32:10Z"
  }
]
```

Notes:
- `amount_paise` is the **net** amount credited to the bank.
- `fees_paise` + `tax_paise` are the Razorpay fee and GST already deducted.
- `linked_account_id` is Razorpay's `acc_xxx` id for the merchant's route
  account — informational, you usually don't need to display it.
- Empty array `[]` = no settlements yet (new merchant) → show empty state.

### 2. Settlement detail — `GET /api/v1/razorpay-settlements/{settlement_id}`

Use the `settlement_id` (e.g. `setl_PabC123XyZ`) from the list.

Response `200` — the same object shape as one list row.
Response `404` — `{ "detail": "settlement not found" }` (not yours / unknown id).

### 3. Per-payment breakdown — `GET /api/v1/razorpay-settlements/{settlement_id}/payments`

Shows which captured payments rolled up into this payout (sourced from the
Razorpay recon report). Use it for the expandable detail / drill-down.

Query params: `limit` (1..2000, default `500`), `offset` (≥0, default `0`).

Response `200`:

```json
[
  {
    "id": 4471,
    "settlement_id": "setl_PabC123XyZ",
    "razorpay_payment_id": "pay_PdEf789",
    "merchant_id": "751c6d1d-1559-45f2-a24b-7ecd16678113",
    "type": "payment",
    "amount_paise": 50000,
    "fee_paise": 118,
    "tax_paise": 21,
    "debit_paise": 0,
    "credit_paise": 49861,
    "created_at": "2026-06-04T09:14:55Z"
  }
]
```

- `type` is typically `payment` (can also be `refund`, `adjustment`, etc.).
- `credit_paise` = what this line added to the payout; `debit_paise` = what it
  subtracted (refunds/adjustments). Net of all lines ≈ settlement `amount_paise`.
- Returns `404` if the parent settlement isn't found for this merchant.

### 4. Force-refresh one settlement — `POST /api/v1/razorpay-settlements/{settlement_id}/sync`

Optional "Refresh" button on the detail view. Re-fetches this settlement from
Razorpay and re-mirrors it, then returns the fresh object (same shape as #2).
Needs `razorpay.settlements.read`. Use sparingly — the backend already polls.

### 5. (Admin / power-user) Recon backfill — `POST /api/v1/razorpay-settlements/recon/sync`

**Not for the normal account-holder screen.** Requires the stronger
`razorpay.recon.run` permission. Pulls a whole month/day of recon data. Only
surface this behind an admin/advanced toggle, if at all.

Body: `{ "year": 2026, "month": 6, "day": 4 }` (`day` optional). Future months
are rejected with `400`.

---

## Suggested React implementation

A typed API client + React Query hooks. Adjust to your existing `apiClient`.

```ts
// types.ts
export type SettlementStatus =
  | 'pending' | 'processing' | 'processed' | 'failed' | 'reversed';

export interface Settlement {
  id: string;
  settlement_id: string;
  merchant_id: string;
  linked_account_id: string | null;
  amount_paise: number;
  fees_paise: number;
  tax_paise: number;
  utr: string | null;
  status: SettlementStatus;
  settled_at: string | null;
  created_for_date: string | null;
  created_at: string;
  updated_at: string;
}

export interface SettlementPayment {
  id: number;
  settlement_id: string;
  razorpay_payment_id: string;
  merchant_id: string;
  type: string;
  amount_paise: number;
  fee_paise: number;
  tax_paise: number;
  debit_paise: number;
  credit_paise: number;
  created_at: string;
}
```

```ts
// api.ts
import { apiClient } from './apiClient'; // injects Authorization header

export interface ListSettlementsParams {
  status?: SettlementStatus;
  from_ts?: string; // ISO
  to_ts?: string;   // ISO
  limit?: number;   // 1..200
  offset?: number;
}

export const settlementsApi = {
  list: (params: ListSettlementsParams = {}) =>
    apiClient
      .get<Settlement[]>('/api/v1/razorpay-settlements', { params })
      .then((r) => r.data),

  get: (settlementId: string) =>
    apiClient
      .get<Settlement>(`/api/v1/razorpay-settlements/${settlementId}`)
      .then((r) => r.data),

  payments: (settlementId: string, limit = 500, offset = 0) =>
    apiClient
      .get<SettlementPayment[]>(
        `/api/v1/razorpay-settlements/${settlementId}/payments`,
        { params: { limit, offset } },
      )
      .then((r) => r.data),

  sync: (settlementId: string) =>
    apiClient
      .post<Settlement>(`/api/v1/razorpay-settlements/${settlementId}/sync`)
      .then((r) => r.data),
};
```

```tsx
// hooks.ts
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { settlementsApi, ListSettlementsParams } from './api';

export const useSettlements = (params: ListSettlementsParams = {}) =>
  useQuery({
    queryKey: ['settlements', params],
    queryFn: () => settlementsApi.list(params),
    staleTime: 60_000,
  });

export const useSettlement = (id: string) =>
  useQuery({
    queryKey: ['settlement', id],
    queryFn: () => settlementsApi.get(id),
    enabled: !!id,
  });

export const useSettlementPayments = (id: string) =>
  useQuery({
    queryKey: ['settlement-payments', id],
    queryFn: () => settlementsApi.payments(id),
    enabled: !!id,
  });

export const useSyncSettlement = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => settlementsApi.sync(id),
    onSuccess: (data) => {
      qc.setQueryData(['settlement', data.settlement_id], data);
      qc.invalidateQueries({ queryKey: ['settlements'] });
    },
  });
};
```

```ts
// format.ts
export const paiseToInr = (p: number) =>
  (p / 100).toLocaleString('en-IN', { style: 'currency', currency: 'INR' });

export const STATUS_LABEL: Record<SettlementStatus, string> = {
  pending: 'Scheduled',
  processing: 'In progress',
  processed: 'Settled',
  failed: 'Failed',
  reversed: 'Reversed',
};

export const STATUS_COLOR: Record<SettlementStatus, string> = {
  pending: 'gray',
  processing: 'blue',
  processed: 'green',
  failed: 'red',
  reversed: 'amber',
};
```

---

## UI checklist

- [ ] Settlements list: status badge, net amount (`amount_paise`), business day
      (`created_for_date`), settled date (`settled_at`), UTR chip when
      `processed`.
- [ ] Status filter dropdown + date range → maps to `status` / `from_ts` /
      `to_ts` query params.
- [ ] Pagination via `limit` / `offset` (or "Load more").
- [ ] Row click → detail drawer/page showing fees (`fees_paise`), tax
      (`tax_paise`), gross = net + fees + tax, and the per-payment breakdown
      from endpoint #3.
- [ ] Copy-to-clipboard on UTR.
- [ ] Empty state when list is `[]`.
- [ ] `403` → show "You don't have access to settlements" (no crash).
- [ ] Optional "Refresh" button on detail → endpoint #4.
- [ ] Do **not** expose recon backfill (#5) on the merchant screen.

## Error handling

| code | meaning                                  | UI                                   |
| ---- | ---------------------------------------- | ------------------------------------ |
| 401  | token missing/expired                    | redirect to login                    |
| 403  | lacks `razorpay.settlements.read`        | locked/empty state                   |
| 404  | settlement id not found for this merchant| "Settlement not found"               |
| 400  | bad params (e.g. future recon month)     | toast the `detail` string            |
