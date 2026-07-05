# Supplier Management — Backend API Contract

Procurement **supplier master** for Bittu POS. Independent of the legacy ERP
`vendors` table (which stays wired to GRN / purchase orders / expense
sub-ledgers). Backed by the `suppliers` table (migration `084_suppliers.sql`).

- Router: `app/api/v1/suppliers.py` — prefix `/api/v1/suppliers`.
- Service: `app/services/supplier_service.py`.
- Tenant-scoped by owner (`user_id`) + optional `branch_id`.
- `supplier_code` is **auto-generated** server-side (`SUP-000001`, …) — read-only.
- `current_outstanding` is **calculated** (starts at `opening_balance`) — never sent by the client.

## Permissions

| Action | Permission |
|---|---|
| read / list | `supplier.read` |
| create / update | `supplier.write` |
| delete | `supplier.delete` |

Owner + Manager roles are granted all three (migration `084` + RBAC fallback).

## Endpoints

| Method | Path | Perm | Purpose |
|---|---|---|---|
| POST   | `/suppliers` | `supplier.write` | Create a supplier |
| GET    | `/suppliers` | `supplier.read` | List (pagination + filter + search) |
| GET    | `/suppliers/{id}` | `supplier.read` | Retrieve one |
| PUT    | `/suppliers/{id}` | `supplier.write` | Full replace |
| PATCH  | `/suppliers/{id}` | `supplier.write` | Partial update |
| DELETE | `/suppliers/{id}` | `supplier.delete` | Soft delete (deactivate) |

### `POST /suppliers`
Required: `supplier_name`, `mobile`. All other fields optional.

```json
{
  "supplier_name": "Fresh Farms",
  "business_name": "Fresh Farms Pvt Ltd",
  "contact_person": "Ravi Kumar",
  "mobile": "9876543210",
  "alternate_phone": "9812345678",
  "email": "ravi@freshfarms.in",
  "address_line1": "12 Market Road",
  "address_line2": "Andheri East",
  "city": "Mumbai",
  "state": "Maharashtra",
  "country": "India",
  "pincode": "400069",
  "gstin": "27AAPFU0939F1ZV",
  "pan": "AAPFU0939F",
  "fssai_license": "11522998000123",
  "category": "vegetables",
  "min_order_value": 500,
  "lead_time_days": 2,
  "is_preferred": true,
  "payment_terms": "credit",
  "credit_period_days": 30,
  "credit_limit": 50000,
  "opening_balance": 1000,
  "bank_name": "HDFC Bank",
  "account_number": "50100123456789",
  "ifsc": "HDFC0001234",
  "upi_id": "freshfarms@hdfcbank",
  "status": "active",
  "notes": "Delivers before 8am"
}
```

Response `201`:
```json
{ "supplier": { "id": "…", "supplier_code": "SUP-000001", "current_outstanding": 1000, "...": "..." }, "created": true }
```

### `GET /suppliers`
Query params: `status` (`active|inactive`), `supplier_name` (ILIKE), `mobile` (ILIKE),
`gstin` (exact, case-insensitive), `search` (free-text across name/business/contact/mobile/email/code),
`limit` (1–200, default 50), `offset` (default 0).

Response `200`:
```json
{ "items": [ { "...": "..." } ], "total": 42, "limit": 50, "offset": 0 }
```

### `GET /suppliers/{id}` → `{ "supplier": { … } }` · `404` if not found.

### `PUT /suppliers/{id}` — full replace
Same required fields as create; omitted optionals reset to defaults/NULL. → `{ "supplier": { … } }`.

### `PATCH /suppliers/{id}` — partial
Send only the fields to change. → `{ "supplier": { … } }`.

### `DELETE /suppliers/{id}` — soft delete → `{ "deleted": true, "id": "…" }`.

## Validation

| Field | Rule |
|---|---|
| `supplier_name` | required, 1–200 chars |
| `mobile` / `alternate_phone` | 10-digit Indian (`+91`/`91`/leading `0` stripped) |
| `email` | basic email; lower-cased |
| `gstin` | 15-char GSTIN; upper-cased |
| `pan` | `AAAAA9999A`; upper-cased |
| `ifsc` | `AAAA0XXXXXX`; upper-cased |
| `pincode` | 6-digit |
| `category` | one of vegetables, dairy, meat_poultry, seafood, grocery, beverages, packaging, cleaning_supplies, other |
| `payment_terms` | cash \| credit \| advance |
| `status` | active \| inactive |
| `min_order_value` / `credit_limit` | ≥ 0 |
| `lead_time_days` / `credit_period_days` | 0–365 |

Errors: `422` validation, `404` not found, `403` permission denied.
