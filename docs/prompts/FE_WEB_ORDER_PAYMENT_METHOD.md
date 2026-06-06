# FE (React Web) — Show "Cash vs Online" for an Order

**Audience:** the React web app (`bittu_web`, Vite + axios + Tailwind).
**Goal:** for any order, display **how it was paid** (Cash / UPI / Card / Wallet / Online via Razorpay) and **whether the money is actually in**.

> ⚠️ **Important — payment data lives on `payments`, NOT on the order row.**
> The `orders` table has no `payment_method` / `payment_status` / `paid_at` columns. All payment facts live in the **`payments`** table (one order can have many rows: split tender, retries).
> Backend now embeds `payments[]` + `payment_summary` directly in `GET /api/v1/orders/{id}` — **one call** is enough.

---

## 1. Source of truth — types

```ts
// src/features/orders/types.ts
export type PaymentMethod = 'cash' | 'upi' | 'card' | 'wallet' | 'online';
export type PaymentStatus =
  | 'pending' | 'initiated' | 'completed' | 'failed'
  | 'refunded' | 'settled' | 'reconciled';

export interface PaymentRow {
  id: string;
  order_id: string;
  method: PaymentMethod;
  status: PaymentStatus;
  amount: number;                       // rupees (numeric(12,2))
  currency: 'INR';
  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;
  paid_at: string | null;               // ISO-8601, set when captured
  created_at: string;
  updated_at: string;
  gateway: string | null;               // 'razorpay' for online; null for cash-likes
  settlement_id: string | null;
  invoice_id: string | null;
}

export type PaymentClass = 'cash' | 'online' | 'mixed' | 'unpaid';

export interface PaymentSummary {
  klass: PaymentClass;
  collected: boolean;
  paid_amount: number;
  refunded_amount: number;
  primary_method: PaymentMethod | null;
  has_gateway_record: boolean;
  latest_paid_at: string | null;
}
```

**Backend semantics (do NOT reinterpret):**
- `method === 'cash'` → POS cash. Money in, no gateway.
- `method` ∈ `'upi' | 'card' | 'wallet'` → cashier-recorded cash-equivalent. **Not** a Razorpay txn. No `razorpay_*` ids.
- `method === 'online'` → Razorpay. Money in only when `status` ∈ `completed | settled | reconciled` AND `razorpay_payment_id` is set.
- One order can have **multiple** payment rows. Always reduce.

---

## 2. Endpoints

### A. `GET /api/v1/orders/{order_id}` — order detail *(canonical, single-call)*

Returns the order row + `items[]` + **`payments[]`** + **`payment_summary`**.

<div align="center">

| Field | Type | Notes |
|:--|:--|:--|
| `id` | uuid | |
| `status` | string | order lifecycle (`pending`, `preparing`, `ready`, `completed`, `Cancelled`) — *not* the payment status |
| `source` | string | `pos` \| `app` \| `qr_table` \| `online` \| `delivery_partner` (column is **`source`**, not `order_source`) |
| `total_amount` | number | rupees |
| `items` / `order_items` | `OrderItem[]` | line items |
| `payments` | `PaymentRow[]` | **one row per cash/online attempt** |
| `payment_summary` | `PaymentSummary` | reduced badge-ready snapshot |
| `customer_name`, `customer_phone` | string\|null | joined from `customers` |
| `metadata` | object | may carry `order_number` |
| `created_at` | string | ISO-8601 |

</div>

### B. `GET /api/v1/orders` — orders list
Returns the same envelope **without** `payments[]` / `payment_summary` today. If you need badges in the list view, either (a) lazy-fetch detail on hover, or (b) call C below per-order.

### C. `GET /api/v1/payments?order_id=<uuid>&order_by=created_at:desc&limit=200`
Drop-in for the list view to grab a single order's payments. Tenant-scoped. `order_id` filter is server-side.

### D. `GET /api/v1/payment-intents/{order_id}` — live Razorpay intent + QR
Only valid when an `online` payment row is `initiated|pending`. Returns `razorpay_order_id`, `qr_image_url`, `qr_close_by`. Don't call for cash-like or already-captured orders.

---

## 3. Local reducer (fallback / list view)

Use `payment_summary` from the backend wherever possible. The reducer below is the **identical** algorithm, kept for the list view where only `payments[]` is available.

```ts
// src/features/orders/paymentMode.ts
import type { PaymentRow, PaymentMethod, PaymentStatus, PaymentSummary } from './types';

const CAPTURED: PaymentStatus[] = ['completed', 'settled', 'reconciled'];

export function summarizeOrderPayments(
  rows: PaymentRow[],
  orderTotal: number,
): PaymentSummary {
  if (!rows?.length) {
    return { klass: 'unpaid', collected: false, paid_amount: 0, refunded_amount: 0,
      primary_method: null, has_gateway_record: false, latest_paid_at: null };
  }
  const captured = rows.filter(r => CAPTURED.includes(r.status));
  const refunded = rows.filter(r => r.status === 'refunded');
  const paid = captured.reduce((s, r) => s + Number(r.amount), 0);
  const refn = refunded.reduce((s, r) => s + Number(r.amount), 0);

  const methods = new Set(captured.map(r => r.method));
  const hasOnline = methods.has('online');
  const hasCashLike = [...methods].some(m => m !== 'online');
  const klass: PaymentClass =
    methods.size === 0 ? 'unpaid' :
    hasOnline && hasCashLike ? 'mixed' :
    hasOnline ? 'online' : 'cash';

  const primary = captured.slice().sort((a, b) => Number(b.amount) - Number(a.amount))[0];
  const latest_paid_at = captured.map(r => r.paid_at).filter(Boolean).sort().reverse()[0] ?? null;

  return {
    klass, collected: paid - refn >= orderTotal - 0.01,
    paid_amount: paid, refunded_amount: refn,
    primary_method: primary?.method ?? null,
    has_gateway_record: rows.some(r => !!r.razorpay_payment_id),
    latest_paid_at,
  };
}

const LABELS: Record<PaymentMethod, string> = {
  cash: 'Cash', upi: 'UPI (manual)', card: 'Card (manual)',
  wallet: 'Wallet (manual)', online: 'Online (Razorpay)',
};
export const paymentMethodLabel = (m: PaymentMethod | null) => (m ? LABELS[m] : '—');

const INR = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 2 });
export const fmtINR = (n: number) => INR.format(n);
```

---

## 4. Hooks

```ts
// src/features/orders/useOrder.ts
import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import type { PaymentRow, PaymentSummary } from './types';

export interface OrderDetail {
  id: string;
  status: string;
  source: string | null;
  total_amount: number;
  items: Array<{ id: string; item_name: string; quantity: number; unit_price: number; total_price: number }>;
  payments: PaymentRow[];               // embedded by backend
  payment_summary: PaymentSummary;      // embedded by backend
  customer_name?: string | null;
  customer_phone?: string | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
}

export function useOrder(orderId: string) {
  return useQuery({
    queryKey: ['order', orderId],
    queryFn: async () => (await api.get<OrderDetail>(`/api/v1/orders/${orderId}`)).data,
    enabled: !!orderId,
    staleTime: 5_000,
  });
}

// Only needed for list/grid screens that don't get payments embedded.
export function useOrderPayments(orderId: string, enabled = true) {
  return useQuery({
    queryKey: ['payments', orderId],
    queryFn: async () =>
      (await api.get<PaymentRow[]>(`/api/v1/payments?order_id=${orderId}&order_by=created_at:desc&limit=200`)).data,
    enabled: enabled && !!orderId,
    staleTime: 5_000,
  });
}
```

---

## 5. Badge component

```tsx
// src/features/orders/PaymentBadge.tsx
import { paymentMethodLabel } from './paymentMode';
import type { PaymentSummary } from './types';

export function PaymentBadge({ summary }: { summary: PaymentSummary }) {
  const tone =
    summary.klass === 'unpaid' ? 'bg-gray-100 text-gray-700 ring-gray-200' :
    summary.klass === 'mixed'  ? 'bg-violet-100 text-violet-800 ring-violet-200' :
    summary.collected          ? 'bg-emerald-100 text-emerald-800 ring-emerald-200' :
                                  'bg-amber-100 text-amber-800 ring-amber-200';

  const label =
    summary.klass === 'unpaid' ? 'Unpaid' :
    summary.klass === 'mixed'  ? 'Split tender' :
                                  paymentMethodLabel(summary.primary_method);

  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}>
      <span>{label}</span>
      <span className="opacity-70">· {summary.collected ? 'Paid' : 'Due'}</span>
    </span>
  );
}
```

---

## 6. Orders list table — centered, modern

Horizontally centered on the page via `mx-auto max-w-6xl`. Rounded card shell, sticky-ready header, zebra-free rows with hover highlight, monetary columns right-aligned with `tabular-nums`.

```tsx
// src/features/orders/OrdersTable.tsx
import { Link } from 'react-router-dom';
import { PaymentBadge } from './PaymentBadge';
import { summarizeOrderPayments, fmtINR } from './paymentMode';
import type { OrderDetail } from './useOrder';

export function OrdersTable({ orders }: { orders: OrderDetail[] }) {
  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-6">
      <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
        <table className="w-full table-fixed border-collapse">
          <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-500">
            <tr>
              <th className="w-[14%] px-4 py-3 text-left">Order #</th>
              <th className="w-[18%] px-4 py-3 text-left">When</th>
              <th className="w-[20%] px-4 py-3 text-left">Customer</th>
              <th className="w-[14%] px-4 py-3 text-center">Source</th>
              <th className="w-[16%] px-4 py-3 text-right">Total</th>
              <th className="w-[18%] px-4 py-3 text-center">Payment</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 text-sm text-gray-800">
            {orders.map(o => {
              const summary = o.payment_summary ?? summarizeOrderPayments(o.payments ?? [], o.total_amount);
              return (
                <tr key={o.id} className="transition-colors hover:bg-gray-50">
                  <td className="truncate px-4 py-3 font-mono text-xs text-gray-600">
                    <Link to={`/orders/${o.id}`} className="text-blue-600 hover:underline">
                      {(o.metadata as any)?.order_number ?? o.id.slice(0, 8).toUpperCase()}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-gray-600">
                    {new Date(o.created_at).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })}
                  </td>
                  <td className="truncate px-4 py-3">
                    {o.customer_name ?? <span className="text-gray-400">Walk-in</span>}
                  </td>
                  <td className="px-4 py-3 text-center text-xs uppercase tracking-wide text-gray-500">
                    {o.source ?? '—'}
                  </td>
                  <td className="px-4 py-3 text-right font-semibold tabular-nums">
                    {fmtINR(Number(o.total_amount))}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <PaymentBadge summary={summary} />
                  </td>
                </tr>
              );
            })}
            {orders.length === 0 && (
              <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400">No orders yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
```

---

## 7. Payments breakdown card — centered, per-order detail

Centered card (`mx-auto max-w-3xl`). Header shows the badge + entry count, body shows each `payments[]` row, footer aggregates Paid / Refunded / Outstanding.

```tsx
// src/features/orders/PaymentsBreakdownCard.tsx
import { paymentMethodLabel, fmtINR } from './paymentMode';
import { PaymentBadge } from './PaymentBadge';
import type { PaymentRow, PaymentSummary } from './types';

const STATUS_TONE: Record<string, string> = {
  completed:  'bg-emerald-50 text-emerald-700 ring-emerald-200',
  settled:    'bg-emerald-50 text-emerald-700 ring-emerald-200',
  reconciled: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  initiated:  'bg-amber-50 text-amber-700 ring-amber-200',
  pending:    'bg-amber-50 text-amber-700 ring-amber-200',
  failed:     'bg-rose-50 text-rose-700 ring-rose-200',
  refunded:   'bg-slate-100 text-slate-700 ring-slate-200',
};

export function PaymentsBreakdownCard({
  rows, summary, orderTotal,
}: { rows: PaymentRow[]; summary: PaymentSummary; orderTotal: number }) {
  const outstanding = Math.max(0, orderTotal - summary.paid_amount + summary.refunded_amount);

  return (
    <div className="mx-auto w-full max-w-3xl px-4 py-6">
      <div className="overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-sm">
        {/* Header */}
        <div className="flex items-center justify-between gap-4 border-b border-gray-100 bg-gradient-to-b from-gray-50 to-white px-5 py-4">
          <div>
            <h3 className="text-sm font-semibold text-gray-700">Payments</h3>
            <p className="mt-0.5 text-xs text-gray-500">
              {rows.length} {rows.length === 1 ? 'entry' : 'entries'} · order total {fmtINR(orderTotal)}
            </p>
          </div>
          <PaymentBadge summary={summary} />
        </div>

        {/* Rows */}
        <table className="w-full border-collapse">
          <thead className="bg-gray-50/50 text-[11px] uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-5 py-2 text-left">Method</th>
              <th className="px-5 py-2 text-center">Status</th>
              <th className="px-5 py-2 text-right">Amount</th>
              <th className="px-5 py-2 text-right">When</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 text-sm">
            {rows.map(r => (
              <tr key={r.id} className="transition-colors hover:bg-gray-50/60">
                <td className="px-5 py-3">
                  <div className="font-medium text-gray-800">{paymentMethodLabel(r.method)}</div>
                  {r.razorpay_payment_id && (
                    <div className="mt-0.5 truncate font-mono text-[11px] text-gray-400">
                      {r.razorpay_payment_id}
                    </div>
                  )}
                </td>
                <td className="px-5 py-3 text-center">
                  <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${STATUS_TONE[r.status] ?? 'bg-gray-100 text-gray-700 ring-gray-200'}`}>
                    {r.status}
                  </span>
                </td>
                <td className={`px-5 py-3 text-right tabular-nums font-semibold ${r.status === 'refunded' ? 'text-rose-600' : 'text-gray-800'}`}>
                  {r.status === 'refunded' ? '−' : ''}{fmtINR(Number(r.amount))}
                </td>
                <td className="px-5 py-3 text-right text-xs text-gray-500">
                  {r.paid_at
                    ? new Date(r.paid_at).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })
                    : <span className="text-gray-400">—</span>}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={4} className="px-5 py-10 text-center text-sm text-gray-400">No payment recorded yet.</td></tr>
            )}
          </tbody>
        </table>

        {/* Footer totals */}
        <div className="grid grid-cols-3 gap-4 border-t border-gray-100 bg-gray-50/60 px-5 py-3 text-xs">
          <div>
            <div className="text-gray-500">Paid</div>
            <div className="mt-0.5 font-semibold tabular-nums text-emerald-700">{fmtINR(summary.paid_amount)}</div>
          </div>
          <div>
            <div className="text-gray-500">Refunded</div>
            <div className="mt-0.5 font-semibold tabular-nums text-rose-600">{fmtINR(summary.refunded_amount)}</div>
          </div>
          <div className="text-right">
            <div className="text-gray-500">Outstanding</div>
            <div className={`mt-0.5 font-semibold tabular-nums ${outstanding > 0 ? 'text-amber-700' : 'text-gray-700'}`}>
              {fmtINR(outstanding)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
```

Usage on the order detail screen:

```tsx
const { data: order, isLoading } = useOrder(orderId);
if (isLoading || !order) return <Skeleton />;
return (
  <PaymentsBreakdownCard
    rows={order.payments}
    summary={order.payment_summary}
    orderTotal={Number(order.total_amount)}
  />
);
```

---

## 8. Live updates
WS event `payment.captured` (channels: `branch:<id>`, `restaurant:<id>`, optional `entity:<order_id>`):
```ts
{ event: 'payment.captured', data: {
    order_id, payment_id, razorpay_payment_id, razorpay_order_id,
    amount, amount_paise, payment_status, merchant_id, branch_id,
    source: 'webhook' | 'poll',
} }
```
On receipt — invalidate both, since `payments[]` lives on the order detail too:
```ts
queryClient.invalidateQueries({ queryKey: ['order', data.order_id] });
queryClient.invalidateQueries({ queryKey: ['payments', data.order_id] });
```

---

## 9. Don'ts
- ❌ Don't read `payment_method` / `payment_status` from the order row — those fields don't exist. Use `payment_summary` or reduce `payments[]`.
- ❌ Don't assume one payment per order. Split tender + retries are real. Always reduce.
- ❌ Don't show "Paid" for an `online` row in `initiated` — money isn't in until `completed` / `settled` / `reconciled`.
- ❌ Don't treat `upi` / `card` / `wallet` rows as gateway transactions — they're cashier-entered. No Razorpay record exists.
- ❌ Don't call `/payment-intents/{order_id}` for cash/upi/card/wallet orders — 404/400.
- ❌ Don't compute "paid" from `orders.total_amount` alone — work from `payments` rows minus refunds.
