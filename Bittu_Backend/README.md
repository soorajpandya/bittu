# Bittu API — OpenAPI 3.1 Modular Specification

## Overview

Enterprise-grade modular OpenAPI specification for the Bittu Restaurant Operating System.

| Metric | Count |
| --- | --- |
| **Total Paths** | 417 |
| **Total Schemas** | 190 |
| **Domains** | 8 |
| **Modules** | 52 |
| **Total Operations** | 512 |

## Directory Structure

```
Bittu_Backend/
  core.yaml                          # Root OpenAPI 3.1 document
  redocly.yaml                       # Redocly CLI config
  components/
    schemas.yaml                     # 190 shared schemas (single source of truth)
    responses.yaml                   # Common HTTP responses (401, 403, 422, 404, 500)
    parameters.yaml                  # Shared query parameters
    security.yaml                    # JWT Bearer security scheme
  modules/
    v1/                              # API version 1
      analytics/
        analytics.yaml          # Analytics
      auth/
        auth.yaml          # Auth
        kyc.yaml          # DigiLocker KYC, KYC / Verification
        staff.yaml          # Staff
      catalog/
        ai-menu.yaml          # AI Ingredients, AI Menu Scanner
        categories.yaml          # Categories
        combos.yaml          # Combos
        food-images.yaml          # Food Images
        items.yaml          # Item Addons, Item Extras, Item Ingredients
        modifiers.yaml          # Modifier Groups, Modifiers
      customers/
        coupons.yaml          # Coupons
        customers.yaml          # Customer Addresses, Customers
        favourites.yaml          # Favourite Items, Favourites
        feedback.yaml          # Feedback
        notifications.yaml          # Notifications
        offers.yaml          # Offers
      finance/
        accounting.yaml          # Accounting, Accounting Rules, Chart of Accounts
        bank-recon.yaml          # Bank Reconciliation
        billing.yaml          # Billing
        cash-transactions.yaml          # Cash Transactions
        due-payments.yaml          # Due Payments
        expenses.yaml          # Expenses
        finance.yaml          # Financial Operating System
        invoices.yaml          # Invoice Import, Invoices
        reports.yaml          # Financial Reports, Reports
        settlements.yaml          # Settlements
        tax.yaml          # Sub-Ledger, Tax Liability
      operations/
        delivery.yaml          # Deliverable Pincodes, Delivery, Delivery Partners
        dinein.yaml          # Dine-In Sessions
        inventory.yaml          # Ingredients, Inventory
        kitchen.yaml          # Kitchen, Kitchen Stations
        orders.yaml          # Orders
        tables.yaml          # Restaurant Tables, Table Events, Table Sessions
        waitlist.yaml          # Waitlist
      payments/
        cashfree.yaml          # Cashfree PG
        payments.yaml          # Payments
        paytm.yaml          # Paytm
        payu.yaml          # PayU
        phonepe.yaml          # PhonePe
        razorpay.yaml          # Razorpay Extended
        webhooks.yaml          # Webhooks
        zivonpay.yaml          # Zivonpay
      platform/
        audit-logs.yaml          # Audit Logs
        erp.yaml          # ERP
        google.yaml          # Google Business Profile
        health.yaml          # Health
        help.yaml          # Help Articles
        misc.yaml          # Miscellaneous
        purchase-orders.yaml          # Purchase Orders
        restaurants.yaml          # Restaurant Settings, Restaurants
        subscriptions.yaml          # Subscriptions
        voice.yaml          # Voice / TTS
    internal/
      _manifest.yaml                 # 45 internal modules
    public/
      _manifest.yaml                 # 7 public modules
```

## Domains

| Domain | Modules | Paths | Operations | Description |
| --- | --- | --- | --- | --- |
| **Analytics** | 1 | 6 | 6 | Analytics & reporting — dashboards, heatmaps, funnels |
| **Auth** | 3 | 33 | 39 | Authentication, staff management, KYC verification |
| **Catalog** | 6 | 39 | 67 | Menu & item management — items, categories, combos, modifiers |
| **Customers** | 6 | 20 | 34 | Customer management — profiles, addresses, coupons, feedback |
| **Finance** | 11 | 140 | 153 | Financial Operating System — accounting, invoices, GST, reports |
| **Operations** | 7 | 71 | 85 | Core restaurant operations — orders, kitchen, tables, delivery |
| **Payments** | 8 | 21 | 22 | Payment gateways — Razorpay, Cashfree, PhonePe, PayU |
| **Platform** | 10 | 87 | 106 | Platform configuration — restaurants, subscriptions, ERP |

## Quick Start

### Preview with Redoc
```bash
npx @redocly/cli preview-docs core.yaml
```

### Bundle into single file
```bash
npx @redocly/cli bundle core.yaml -o bundled.yaml
```

### Lint the spec
```bash
npx @redocly/cli lint core.yaml
```

### Validate with Swagger UI
```bash
npx @redocly/cli bundle core.yaml -o bundled.yaml
# Open bundled.yaml in https://editor.swagger.io
```

## Versioning Strategy

- Current: `modules/v1/` — all paths under `/api/v1/`
- Future: `modules/v2/` — breaking changes only, v1 preserved
- Module files include `x-module-info.version` for tracking

## Visibility

Modules are classified as **public** or **internal**:
- **Public**: Endpoints accessible without JWT (OAuth flows, QR scans, health checks, webhooks)
- **Internal**: All other endpoints requiring authenticated JWT

See `modules/internal/_manifest.yaml` and `modules/public/_manifest.yaml`.

## Schema References

All schemas live in `components/schemas.yaml` (single source of truth).
Module files reference schemas via:
```yaml
$ref: '../../../components/schemas.yaml#/SchemaName'
```

Within `components/schemas.yaml`, internal cross-references use:
```yaml
$ref: '#/OtherSchemaName'
```

## Contributing

1. Add new paths to the appropriate `modules/v1/{domain}/{module}.yaml`
2. Add new schemas to `components/schemas.yaml`
3. Run `npx @redocly/cli lint core.yaml` before committing
4. Bundle with `npx @redocly/cli bundle core.yaml -o bundled.yaml` for deployment
