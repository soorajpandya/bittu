# FE (React Web) — Show "Cash vs Online" for an Order

**Audience:** the React web app (`bittu_web`, Vite + axios).
**Goal:** for any order, display **how it was paid** (Cash / UPI / Card / Wallet / Online via Razorpay) and **whether the money is actually in**.

> ⚠️ **Important — payment data lives on `payments`, NOT on the order row.**
> The `orders` table has no `payment_method` / `payment_status` / `paid_at` columns. All payment facts live in the **`payments`** table (one order can have many payment rows: split tender, retries). The FE MUST read from a payments-aware endpoint.

---

## 1. Source of truth — `payments` rows

```ts
export type PaymentMethod = 'cash' | 'upi' | 'card' | 'wallet' | 'online';
export type PaymentStatus =
  | 'pending' | 'initiated' | 'completed' | 'failed'
  | 'refunded' | 'settled' | 'reconciled';

export interface PaymentRow {
  id: string;
  order_id: string;
  restaurant_id: string | null;
  branch_id: string | null;
  method: PaymentMethod;
  status: PaymentStatus;
  amount: number;            // rupees (numeric(12,2))
  currency: 'INR';
  razorpay_order_id: string | null;
  razorpay_payment_id: string | null;
  paid_at: string | null;    // ISO-8601, set when status reaches completed
  created_at: string;
  updated_at: string;
  gateway: string | null;    // 'razorpay' for online; null for cash-likes
  settlement_id: string | null;
  invoice_id: string | null;
}
```

**Backend semantics (do NOT reinterpret):**
- `method === 'cash'` → POS cash. Money in, no gateway.
- `method` ∈ `'upi' | 'card' | 'wallet'` → cashier-recorded cash-equivalent. **Not** a Razorpay txn. No `razorpay_*` ids. Treat as cash for UX, but show the label.
- `method === 'online'` → Razorpay. Money in only when `status` is `completed | settled | reconciled` AND `razorpay_payment_id` is set.
- One order can have **multiple** payment rows. Always reduce.

---

## 2. Endpoints

### A. `GET /api/v1/orders/{order_id}` — order detail
Returns the `orders` row + `items[]`. **Does NOT include payments today.** Use it for line items, totals, status, customer info.

Order columns the FE actually receives:

| Field | Type | Notes |
|---|---|---|
| `id` | uuid | |
| `status` | string | order lifecycle (`pending`, `preparing`, `ready`, `completed`, `Cancelled`, ...) — **not the payment status** |
| `source` | string | `pos` \| `app` \| `qr_table` \| `online` \| `delivery_partner` (column is **`source`**, not `order_source`) |
| `total_amount` | number | rupees |
| `items` / `order_items` | array | line items |
| `customer_name`, `customer_phone` | string\|null | joined from `customers` |
| `metadata` | object | may carry `order_number` |
| `created_at` | string | ISO-8601 |

### B. `GET /api/v1/payments?order_by=created_at:desc&limit=&offset=` — payments list
**Canonical source for cash vs online.** Tenant-scoped. Filter client-side by `order_id` (or use `?order_id=` once backend adds it).

### C. `GET /api/v1/payment-intents/{order_id}` — live Razorpay intent + QR
Only valid when an `online` payment row is `initiated|pending`. Returns `razorpay_order_id`, `qr_image_url`, `qr_close_by`. Don't call for cash-like or already-captured orders.

---

## 3. Reducing multiple payments → one badge

```ts
// src/features/orders/paymentMode.ts

export type PaymentClass = 'cash' | 'online' | 'mixed' | 'unpaid';

export interface OrderPaymentSummary {
  klass: PaymentClass;
  collected: boolean;        // money fully in?
  paidAmount: number;        // sum of captured rows
  refundedAmount: number;
  primaryMethod: PaymentMethod | null;
  hasGatewayRecord: boolean; // any razorpay_payment_id
  latestPaidAt: string | null;
}

const CAPTURED: PaymentStatus[] = ['completed', 'settled', 'reconciled'];
const isOnline = (m: PaymentMethod) => m === 'online';

export function summarizeOrderPayments(
  rows: PaymentRow[],
  orderTotal: number,
): OrderPaymentSummary {
  if (rows.length === 0) {
    return { klass: 'unpaid', collected: false, paidAmount: 0,
      refundedAmount: 0, primaryMethod: null, hasGatewayRecord: false, latestPaidAt: null };
  }
  const captured = rows.filter(r => CAPTURED.includes(r.status));
  const refunded = rows.filter(r => r.status === 'refunded');
  const paidAmount = captured.reduce((s, r) => s + Number(r.amount), 0);
  const refundedAmount = refunded.reduce((s, r) => s + Number(r.amount), 0);
  const collected = paidAmount - refundedAmount >= orderTotal - 0.01;

  const methods = new Set(captured.map(r => r.method));
  const hasOnline = [...methods].some(isOnline);
  const hasCashLike = [...methods].some(m => !isOnline(m));
  const klass: PaymentClass =
    methods.size === 0 ? 'unpaid' :
    hasOnline && hasCashLike ? 'mixed' :
    hasOnline ? 'online' : 'cash';

  const primary = captured.slice().sort((a, b) => Number(b.amount) - Number(a.amount))[0];
  const latestPaidAt = captured.map(r => r.paid_at).filter(Boolean).sort().reverse()[0] ?? null;

  return {
    klass, collected, paidAmount, refundedAmount,
    primaryMethod: primary?.method ?? null,
    hasGatewayRecord: rows.some(r => !!r.razorpay_payment_id),
    latestPaidAt,
  };
}

const LABELS: Record<PaymentMethod, string> = {
  cash: 'Cash',
  upi: 'UPI (manual)',
  card: 'Card (manual)',
  wallet: 'Wallet (manual)',
  online: 'Online (Razorpay)',
};
export const paymentMethodLabel = (m: PaymentMethod | null) => (m ? LABELS[m] : '—');
```

---

## 4. Hooks

```ts
// src/features/orders/useOrder.ts
import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';

export interface OrderDetail {
  id: string;
  status: string;
  source: string | null;
  total_amount: number;
  items: Array<{ id: string; item_name: string; quantity: number; unit_price: number }>;
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
  });
}

export function useOrderPayments(orderId: string) {
  return useQuery({
    queryKey: ['payments', orderId],
    queryFn: async () => {
      const { data } = await api.get<{ items?: PaymentRow[] } | PaymentRow[]>(
        `/api/v1/payments?order_by=created_at:desc&limit=200`,
      );
      const list: PaymentRow[] = Array.isArray(data) ? data : (data.items ?? []);
      return list.filter(r => r.order_id === orderId);
    },
    enabled: !!orderId,
    staleTime: 5_000,
  });
}
```

## 5. Badge component

```tsx
// src/features/orders/PaymentBadge.tsx
import { summarizeOrderPayments, paymentMethodLabel } from './paymentMode';

export function PaymentBadge({ rows, orderTotal }: {
  rows: PaymentRow[]; orderTotal: number;
}) {
  const s = summarizeOrderPayments(rows, orderTotal);

  const tone =
    s.klass === 'unpaid' ? 'bg-gray-100 text-gray-700' :
    s.klass === 'mixed'  ? 'bg-violet-100 text-violet-800' :
    s.collected          ? 'bg-emerald-100 text-emerald-800' :
                           'bg-amber-100 text-amber-800';

  const label =
    s.klass === 'unpaid' ? 'Unpaid' :
    s.klass === 'mixed'  ? 'Split tender' :
                            paymentMethodLabel(s.primaryMethod);

  return (
    <span className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs ${tone}`}>
      <span className="font-medium">{label}</span>
      <span className="opacity-70">· {s.collected ? 'Paid' : 'Due'}</span>
    </span>
  );
}
```

Usage:
```tsx
const { data: order }    = useOrder(orderId);
const { data: payments } = useOrderPayments(orderId);
return <PaymentBadge rows={payments ?? []} orderTotal={order?.total_amount ?? 0} />;
```

---

## 6. Live updates
WS event `payment.captured` (channels: `branch:<id>`, `restaurant:<id>`, optional `entity:<order_id>`):
```ts
{ event: 'payment.captured', data: {
    order_id, payment_id, razorpay_payment_id, razorpay_order_id,
    amount, amount_paise, payment_status, merchant_id, branch_id,
    source: 'webhook' | 'poll',
} }
```
On receipt:
```ts
queryClient.invalidateQueries({ queryKey: ['payments', data.order_id] });
```

---

## 7. Don'ts
- ❌ Don't read `payment_method` / `payment_status` from `GET /orders/{id}` — those fields don't exist on the order row. They live on `payments`.
- ❌ Don't assume one payment per order. Split tender + retries are real. Always reduce.
- ❌ Don't show "Paid" for an `online` row in `initiated` — money isn't in until `completed`/`settled`/`reconciled`.
- ❌ Don't treat `upi` / `card` / `wallet` rows as gateway transactions — they're cashier-entered. No Razorpay record exists.
- ❌ Don't call `/payment-intents/{order_id}` for cash/upi/card/wallet orders — 404/400.
- ❌ Don't compute "paid" from `orders.total_amount` alone — work from `payments` rows minus refunds.
