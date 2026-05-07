# Bittu POS — Flutter Frontend Implementation Guide

## For: Flutter Developer | From: Backend Architect
## Version: v2 | Date: May 2026
## See also: [FLUTTER_ENTERPRISE_ARCHITECTURE.md](./FLUTTER_ENTERPRISE_ARCHITECTURE.md) — required reading

---

## ⚡ CRITICAL CONTEXT

You are building the Flutter frontend for **Bittu POS** — a real-time restaurant operating system.
The backend is **fully built and deployed**. Your job is to consume these APIs.

**Production Base URL**: `https://api.bittupos.com` (or the EC2 URL provided)
**Local Dev URL**: `http://localhost:8000`
**All API prefix**: `/api/v1`
**Docs (live)**: `{BASE_URL}/docs` (Swagger UI) | `{BASE_URL}/redoc`

---

## 1. AUTHENTICATION (Google OAuth via Supabase)

### Flow (step-by-step for Flutter):

```
Step 1: GET /api/v1/auth/google?redirect_to=https://yourapp.com/callback
        → Returns: {"url": "https://xxx.supabase.co/auth/v1/authorize?..."}

Step 2: Open that URL in WebView / Custom Chrome Tab
        → User signs in with Google
        → Supabase redirects to your redirect_to URL with ?code=XXXXXX

Step 3: POST /api/v1/auth/google/callback?code=XXXXXX
        → Returns: {
            "access_token": "eyJhbG...",
            "refresh_token": "xxxx",
            "expires_in": 3600,
            "user": {"id": "uuid", "email": "user@gmail.com", ...}
          }

Step 4: Store access_token + refresh_token securely (flutter_secure_storage)

Step 5: GET /api/v1/auth/me  (Header: Authorization: Bearer <access_token>)
        → Returns: {
            "id": "uuid",
            "email": "user@gmail.com",
            "role": "owner",          // owner|manager|cashier|chef|waiter|staff
            "restaurant_id": "uuid",
            "branch_id": "uuid",
            "owner_id": "uuid",
            "is_branch_user": false   // true = staff login, false = owner login
          }

Step 6: GET /api/v1/auth/permissions/me
        → Returns: {
            "role": "manager",
            "permissions": {
              "order.create": {"allowed": true, "meta": {}},
              "billing.discount": {"allowed": true, "meta": {"max_discount_percent": 50}},
              "menu.read": {"allowed": true, "meta": {}},
              "menu.delete": {"allowed": false, "meta": {}},
              ...
            }
          }
        → USE THIS TO SHOW/HIDE UI ELEMENTS
```

### Token Refresh (call when 401 received):
```
POST /api/v1/auth/token/refresh
Body: {"refresh_token": "stored_refresh_token"}
→ Returns new access_token + refresh_token
```

### Logout:
```
POST /api/v1/auth/logout
Header: Authorization: Bearer <token>
```

### EVERY authenticated request needs this header:
```
Authorization: Bearer <access_token>
Content-Type: application/json
```

---

## 2. ROLE-BASED UI ADAPTATION

### Role Hierarchy (highest → lowest):
```
owner (100) → manager (80) → cashier (60) → chef (40) → waiter (30) → staff (20)
```

### What each role sees:

| Screen | Owner | Manager | Cashier | Waiter | Chef | Staff |
|--------|-------|---------|---------|--------|------|-------|
| Dashboard | ✅ Full | ✅ Full | ❌ | ❌ | ❌ | ❌ |
| Orders | ✅ CRUD | ✅ CRUD | ✅ Read+Edit | ✅ Create+Read | ✅ Read | ✅ Read |
| Menu/Items | ✅ CRUD | ✅ CRU (no delete) | ✅ Read | ❌ | ❌ | ❌ |
| Kitchen | ✅ | ✅ | ❌ | ✅ Read | ✅ Full | ❌ |
| Tables | ✅ CRUD | ✅ CRUD | ✅ Manage | ✅ Manage | ❌ | ✅ Read |
| Staff Mgmt | ✅ Full | ❌ | ❌ | ❌ | ❌ | ❌ |
| Finance | ✅ Full | ✅ Full | ✅ Cash only | ❌ | ❌ | ❌ |
| Analytics | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Settings | ✅ Full | ✅ Read | ❌ | ❌ | ❌ | ❌ |
| Subscriptions | ✅ Full | ❌ | ❌ | ❌ | ❌ | ❌ |

### Flutter Implementation:
```dart
// After login, store permissions map globally
final perms = await api.getPermissions(); // GET /auth/permissions/me

// Use helper to check
bool canAccess(String key) => perms[key]?['allowed'] == true;

// In UI:
if (canAccess('order.create')) ShowCreateOrderButton();
if (canAccess('menu.delete')) ShowDeleteItemButton();
if (canAccess('billing.discount')) {
  final maxDiscount = perms['billing.discount']['meta']['max_discount_percent'];
  // Cap the discount slider to maxDiscount
}
```

---

## 3. COMPLETE API REFERENCE

### 3.1 Restaurants

```
GET  /api/v1/restaurants                    → List owner's restaurants
POST /api/v1/restaurants                    → Create restaurant (auto-done on first login)
PATCH /api/v1/restaurants/{restaurant_id}   → Update restaurant

Body for PATCH:
{
  "name": "My Restaurant",
  "phone": "+91...",
  "email": "rest@email.com",
  "address": "...",
  "city": "Mumbai",
  "state": "Maharashtra",
  "pincode": "400001",
  "lat": 19.076,
  "lng": 72.877,
  "logo_url": "https://...",
  "cover_url": "https://...",
  "gst_number": "27AAACR5055K1Z5",
  "fssai_number": "12345678901234",
  "is_active": true,
  "opening_time": "09:00",
  "closing_time": "23:00",
  "avg_prep_time": 25
}
```

### 3.2 Restaurant Settings

```
GET  /api/v1/restaurant-settings            → Get settings
PUT  /api/v1/restaurant-settings            → Update settings
```

### 3.3 Categories

```
GET    /api/v1/categories?active_only=true
POST   /api/v1/categories
       Body: {"name": "Starters", "slug": "starters", "description": "...", "image_url": "...", "sort_order": 1, "is_active": true}
GET    /api/v1/categories/{category_id}
PATCH  /api/v1/categories/{category_id}     → Partial update (any field)
DELETE /api/v1/categories/{category_id}
```

### 3.4 Menu Items

```
GET    /api/v1/items                        → List all items
POST   /api/v1/items
       Body: {
         "Item_Name": "Paneer Tikka",
         "price": 299.0,
         "Description": "...",
         "Category": "Starters",
         "Subcategory": "Veg",
         "Cuisine": "North Indian",
         "Spice_Level": "Medium",
         "Prep_Time_Min": 15,
         "Image_url": "https://...",
         "is_veg": true,
         "tags": ["bestseller", "spicy"],
         "sort_order": 1,
         "dine_in_available": true,
         "takeaway_available": true,
         "delivery_available": true
       }
POST   /api/v1/items/bulk-import           → Bulk create
       Body: {"items": [ItemCreate, ...], "skip_duplicates": true}
GET    /api/v1/items/{item_id}
PATCH  /api/v1/items/{item_id}              → Partial update
PUT    /api/v1/items/{item_id}              → Full update
DELETE /api/v1/items/{item_id}

// Item sub-resources:
GET    /api/v1/items/{item_id}/variants
GET    /api/v1/items/{item_id}/addons
GET    /api/v1/items/{item_id}/extras
GET    /api/v1/items/{item_id}/station-mappings
GET    /api/v1/items/{item_id}/modifier-groups
```

### 3.5 Item Variants

```
GET    /api/v1/item-variants
POST   /api/v1/item-variants                → {"item_id": "...", "name": "Large", "price": 349}
PATCH  /api/v1/item-variants/{variant_id}
DELETE /api/v1/item-variants/{variant_id}
```

### 3.6 Item Addons

```
GET    /api/v1/item-addons
POST   /api/v1/item-addons                  → {"item_id": "...", "name": "Extra Cheese", "price": 49}
PATCH  /api/v1/item-addons/{addon_id}
DELETE /api/v1/item-addons/{addon_id}
```

### 3.7 Item Extras

```
GET    /api/v1/item-extras
POST   /api/v1/item-extras                  → {"item_id": "...", "name": "Butter Naan", "price": 59}
PATCH  /api/v1/item-extras/{extra_id}
DELETE /api/v1/item-extras/{extra_id}
```

### 3.8 Modifier Groups (Customizations)

```
GET    /api/v1/modifier-groups
POST   /api/v1/modifier-groups              → {"name": "Size", "required": true, "multi_select": false, "min_select": 1, "max_select": 1}
GET    /api/v1/modifier-groups/{group_id}
PATCH  /api/v1/modifier-groups/{group_id}
DELETE /api/v1/modifier-groups/{group_id}
POST   /api/v1/modifier-groups/{group_id}/options  → {"name": "Regular", "price_adjustment": 0}
PATCH  /api/v1/modifier-groups/options/{option_id}
DELETE /api/v1/modifier-groups/options/{option_id}
```

### 3.9 Combos

```
GET    /api/v1/combos
POST   /api/v1/combos
       Body: {
         "name": "Family Pack",
         "description": "...",
         "price": 999,
         "items": [{"item_id": "uuid", "quantity": 2}],
         "image_url": "..."
       }
GET    /api/v1/combos/items                 → All combos with their items
GET    /api/v1/combos/{combo_id}
PATCH  /api/v1/combos/{combo_id}
DELETE /api/v1/combos/{combo_id}
```

---

### 3.10 Orders (CORE)

```
POST   /api/v1/orders
       Body: {
         "items": [
           {
             "item_id": "uuid_or_int",
             "item_name": "Paneer Tikka",
             "variant_id": "uuid_or_null",
             "quantity": 2,
             "price": 299.0,
             "addons": [],
             "notes": "Extra spicy"
           }
         ],
         "order_type": "dine_in",           // dine_in | takeaway | delivery | qr_dine_in
         "table_id": "uuid_or_null",
         "customer_name": "John",
         "customer_phone": "+91...",
         "delivery_address": "...",
         "notes": "Ring the bell",
         "source": "pos",                   // pos | app | web | qr
         "idempotency_key": "unique_key"    // Prevent duplicate orders
       }
       → Returns: created order object

GET    /api/v1/orders?branch_id=&status=&order_type=&from_date=&to_date=&page=1&page_size=50
GET    /api/v1/orders/{order_id}
PATCH  /api/v1/orders/{order_id}            → Update (status, notes)
       Body: {"status": "confirmed", "notes": "Updated"}
PUT    /api/v1/orders/{order_id}            → Full update
DELETE /api/v1/orders/{order_id}            → Cancels order
PATCH  /api/v1/orders/{order_id}/status     → Status transition only
       Body: {"status": "preparing"}
POST   /api/v1/orders/{order_id}/discount   → Apply discount
       Body: {"discount_percent": 10, "reason": "Loyalty customer"}
       ⚠️ Capped by role's max_discount_percent from permissions meta
```

**Order Status Flow:**
```
placed → confirmed → preparing → ready → served → completed
                                                 → cancelled (from any state)
```

---

### 3.11 Payments

```
GET    /api/v1/payments?order_by=created_at&limit=50&offset=0
POST   /api/v1/payments
       Body: {"order_id": "uuid", "method": "cash|upi|card|wallet|online", "amount": 599}

POST   /api/v1/payments/initiate            → Start online payment
       Body: {"order_id": "uuid", "payment_mode": "cash|online", "amount": 599, "tip": 50}

POST   /api/v1/payments/verify              → Verify Razorpay payment
       Body: {"order_id": "uuid", "razorpay_payment_id": "...", "razorpay_order_id": "...", "razorpay_signature": "..."}

POST   /api/v1/payments/refund
       Body: {"payment_id": "uuid", "amount": 200, "reason": "Item not available"}

POST   /api/v1/payments/voice               → Voice-based payment notification
```

### 3.12 Kitchen Display

```
GET    /api/v1/kitchen/active?station_id=&status=
       → Returns active orders with items grouped for kitchen

PATCH  /api/v1/kitchen/orders/{order_id}/status
       Body: {"status": "preparing|ready|served"}

PATCH  /api/v1/kitchen/items/{item_id}/status
       Body: {"status": "preparing|ready"}
       → Per-item status updates

GET    /api/v1/kitchen/stations/{station_id}
       → Station-filtered view
```

### 3.13 Kitchen Stations

```
GET    /api/v1/kitchen-stations
POST   /api/v1/kitchen-stations
       Body: {"name": "Tandoor", "type": "hot"}
```

### 3.14 Tables

```
GET    /api/v1/tables                       → All tables with status
POST   /api/v1/tables
       Body: {"table_number": "T1", "capacity": 4, "status": "available", "is_active": true}
PATCH  /api/v1/tables/{table_id}
DELETE /api/v1/tables/{table_id}

// Table Sessions
POST   /api/v1/tables/sessions              → Start session
POST   /api/v1/tables/sessions/join         → Join existing session
POST   /api/v1/tables/cart/add              → Add to cart
GET    /api/v1/tables/cart/{session_id}     → Get cart
DELETE /api/v1/tables/cart/remove           → Remove from cart
POST   /api/v1/tables/sessions/{session_id}/end → End session

// QR-Based Table Ordering (PUBLIC - no JWT):
POST   /api/v1/tables/qr/scan              → Scan QR, get session
GET    /api/v1/tables/qr/menu              → Public menu
GET    /api/v1/tables/qr/cart              → Get QR cart
POST   /api/v1/tables/qr/cart              → Add to QR cart
POST   /api/v1/tables/qr/place-order       → Place QR order
GET    /api/v1/tables/qr/order-status       → Check order status
POST   /api/v1/tables/qr/call-waiter       → Call waiter
GET    /api/v1/tables/sessions/{session_id}/bill
POST   /api/v1/tables/sessions/{session_id}/split-bill
POST   /api/v1/tables/sessions/{session_id}/payments
POST   /api/v1/tables/sessions/{session_id}/paid-vacate
```

---

### 3.15 Dine-In (QR Session Engine) — PUBLIC ENDPOINTS

**These do NOT need JWT. They use `session_token` from QR scan.**

```
POST  /api/v1/dinein/qr/scan
      Body: {"restaurant_id": "uuid", "table_id": "uuid", "device_id": "device123", "session_token": null}
      → Returns: {"session_id": "uuid", "session_token": "token123", "table": {...}, "status": "active"}
      ⚠️ If session_token provided and valid, returns existing session

GET   /api/v1/dinein/qr/session?session_token=TOKEN
      → Full session state (table, cart, orders, payments)

GET   /api/v1/dinein/qr/menu?restaurant_id=UUID
      → Full menu with categories, items, variants, addons

POST  /api/v1/dinein/qr/cart/add
      Body: {"session_token": "...", "item_id": "...", "quantity": 1, "variant_id": "...", "addons": [], "extras": [], "notes": "...", "device_id": "...", "request_id": "unique"}

POST  /api/v1/dinein/qr/cart/update
      Body: {"session_token": "...", "cart_item_id": "...", "quantity": 2}

POST  /api/v1/dinein/qr/cart/remove
      Body: {"session_token": "...", "cart_item_id": "..."}

POST  /api/v1/dinein/qr/cart/clear?session_token=TOKEN

GET   /api/v1/dinein/qr/cart?session_token=TOKEN

POST  /api/v1/dinein/qr/place-order
      Body: {"session_token": "...", "device_id": "...", "notes": "...", "customer_name": "...", "customer_phone": "...", "payment_method": "cash|online", "request_id": "unique"}

GET   /api/v1/dinein/qr/order-status?session_token=TOKEN

POST  /api/v1/dinein/qr/merge
      Body: {"source_session_token": "...", "target_session_token": "..."}

POST  /api/v1/dinein/qr/call-waiter
      Body: {"session_token": "...", "request_type": "assistance|bill|water"}

POST  /api/v1/dinein/qr/close-session
      Body: {"session_token": "...", "reason": "completed"}
```

**Admin Dine-In (JWT required):**
```
GET   /api/v1/dinein/admin/kitchen-view     → Kitchen display grouped by table
GET   /api/v1/dinein/sessions/{id}/bill     → Bill snapshot
POST  /api/v1/dinein/sessions/{id}/split-bill
      Body: {"split_type": "equal|by_item|by_user", "parts": 3, "item_splits": [], "user_splits": []}
POST  /api/v1/dinein/sessions/{id}/payments
      Body: {"amount": 500, "payment_method": "cash|upi|card", "transaction_ref": "...", "paid_by": "...", "notes": "..."}
POST  /api/v1/dinein/sessions/{id}/paid-vacate   → Close + free table
```

---

### 3.16 Customers

```
GET    /api/v1/customers?search=&limit=50&offset=0
POST   /api/v1/customers
       Body: {"name": "John", "phone": "+91...", "email": "...", "address": "..."}
GET    /api/v1/customers/{customer_id}
PATCH  /api/v1/customers/{customer_id}
DELETE /api/v1/customers/{customer_id}

// Addresses
GET    /api/v1/customer-addresses/{customer_id}
POST   /api/v1/customer-addresses/{customer_id}
       Body: {"label": "Home", "address_line": "...", "city": "...", "state": "...", "pincode": "...", "lat": 0, "lng": 0, "is_default": true}
PATCH  /api/v1/customer-addresses/address/{address_id}
DELETE /api/v1/customer-addresses/address/{address_id}
```

### 3.17 Delivery

```
GET    /api/v1/delivery                     → List deliveries
POST   /api/v1/delivery
       Body: {"order_id": "uuid", "customer_name": "...", "customer_phone": "...", "address": "...", "lat": 0, "lng": 0}
PATCH  /api/v1/delivery/{delivery_id}/assign
       Body: {"partner_id": "uuid"}
PATCH  /api/v1/delivery/{delivery_id}/status
       Body: {"status": "picked_up|in_transit|delivered|failed"}
POST   /api/v1/delivery/{delivery_id}/location
       Body: {"lat": 19.076, "lng": 72.877}

// Delivery Partners
GET    /api/v1/delivery-partners
POST   /api/v1/delivery-partners            → {"name": "...", "phone": "...", "vehicle_type": "bike"}
PATCH  /api/v1/delivery-partners/{partner_id}
DELETE /api/v1/delivery-partners/{partner_id}

// Pincodes
GET    /api/v1/pincodes
POST   /api/v1/pincodes                    → {"pincode": "400001", "area": "Colaba", "delivery_charge": 30}
DELETE /api/v1/pincodes/{pincode_id}
```

### 3.18 Staff Management

```
// Branches
GET    /api/v1/staff/branches
POST   /api/v1/staff/branches               → {"name": "Downtown Branch", "address": "...", "phone": "..."}
PATCH  /api/v1/staff/branches/{branch_id}

// Branch Users (people who can LOG IN with Google)
GET    /api/v1/staff/branch-users/me        → Current user's record
GET    /api/v1/staff/branch-users?branch_id=
POST   /api/v1/staff/branch-users           → {"branch_id": "uuid", "user_id": "uuid", "role": "manager"}
PATCH  /api/v1/staff/branch-users/{user_id} → {"role": "cashier"}
DELETE /api/v1/staff/branch-users/{user_id}

// Local Staff (no login — tracked in system)
POST   /api/v1/staff                         → {"name": "Ram", "role": "waiter", "phone": "+91...", "branch_id": "uuid"}
GET    /api/v1/staff?branch_id=
PATCH  /api/v1/staff/{staff_id}
DELETE /api/v1/staff/{staff_id}

// Invites (invite by email → auto-accepted on their next login)
POST   /api/v1/staff/invites                → {"branch_id": "uuid", "email": "staff@gmail.com", "role": "cashier"}
GET    /api/v1/staff/invites?branch_id=&status=pending|accepted|revoked|expired
DELETE /api/v1/staff/invites/{invite_id}    → Revoke
```

### 3.19 Coupons & Offers

```
// Coupons
GET    /api/v1/coupons
POST   /api/v1/coupons                      → {"code": "WELCOME50", "discount_type": "percent", "discount_value": 50, "min_order_value": 200, "max_discount": 100, "valid_from": "...", "valid_to": "...", "usage_limit": 100}
GET    /api/v1/coupons/{coupon_id}
PATCH  /api/v1/coupons/{coupon_id}
DELETE /api/v1/coupons/{coupon_id}
GET    /api/v1/coupons/{coupon_id}/usage

// Offers
GET    /api/v1/offers
POST   /api/v1/offers                       → {"name": "Happy Hour", "description": "...", "discount_type": "percent", "discount_value": 20, "valid_from": "...", "valid_to": "...", "is_active": true}
GET    /api/v1/offers/{offer_id}
PATCH  /api/v1/offers/{offer_id}
DELETE /api/v1/offers/{offer_id}
```

### 3.20 Feedback

```
GET    /api/v1/feedback
POST   /api/v1/feedback                     → {"order_id": "uuid", "rating": 5, "comment": "Great food!", "tags": ["fast", "tasty"]}
GET    /api/v1/feedback/{feedback_id}
DELETE /api/v1/feedback/{feedback_id}
PATCH  /api/v1/feedback/{feedback_id}/respond → {"response": "Thank you!"}
```

### 3.21 Favourites

```
GET    /api/v1/favourites
POST   /api/v1/favourites                   → {"item_id": "uuid"}
DELETE /api/v1/favourites/{item_id}
GET    /api/v1/favourite-items              → Items in favourites with full details
```

### 3.22 Notifications

```
GET    /api/v1/notifications                → List notifications
GET    /api/v1/notifications/alerts         → Alert-type notifications
PATCH  /api/v1/notifications/alerts/{alert_id}/read
PATCH  /api/v1/notifications/alerts/read-all
DELETE /api/v1/notifications/alerts/{alert_id}
```

### 3.23 Analytics

```
GET  /api/v1/analytics/dashboard-counts?branch_id=  → Today's counts (orders, revenue, avg_order_value)
GET  /api/v1/analytics/daily?date=2026-04-20&branch_id=  → Daily breakdown
GET  /api/v1/analytics/dashboard                     → Full dashboard data
GET  /api/v1/analytics/compare?from=&to=&compare_from=&compare_to=  → Period comparison
GET  /api/v1/analytics/heatmap                       → Order heatmap by hour
POST /api/v1/analytics/funnel                        → Conversion funnel
```

### 3.24 Waitlist

```
POST   /api/v1/waitlist                     → {"customer_name": "...", "party_size": 4, "phone": "..."}
GET    /api/v1/waitlist                     → Active waitlist
GET    /api/v1/waitlist/stats               → Queue statistics
GET    /api/v1/waitlist/history             → Past entries
POST   /api/v1/waitlist/notify-next         → Notify next in queue
POST   /api/v1/waitlist/expire-check        → Run expiry check
POST   /api/v1/waitlist/{entry_id}/seat     → Seat the party
POST   /api/v1/waitlist/{entry_id}/skip     → Skip this entry
PATCH  /api/v1/waitlist/{entry_id}/cancel   → Cancel
PUT    /api/v1/waitlist/reorder             → Reorder queue

// Settings
GET    /api/v1/waitlist/settings
PUT    /api/v1/waitlist/settings

// Public (no JWT)
GET    /api/v1/waitlist/display/{restaurant_id}  → Public display board
GET    /api/v1/waitlist/status/{entry_id}         → Public status check
```

### 3.25 Subscriptions & Billing

```
GET    /api/v1/subscriptions/plans          → PUBLIC - list plans
GET    /api/v1/subscriptions/addons         → PUBLIC - list add-ons
GET    /api/v1/subscriptions/status         → Check if active
GET    /api/v1/subscriptions                → Full subscription details
POST   /api/v1/subscriptions/subscribe      → {"plan_slug": "starter|growth|pro"}
POST   /api/v1/subscriptions/verify         → Verify payment
POST   /api/v1/subscriptions/free-trial     → Start free trial
POST   /api/v1/subscriptions/upgrade        → {"new_plan_slug": "pro"}
POST   /api/v1/subscriptions/downgrade      → {"new_plan_slug": "starter"}
POST   /api/v1/subscriptions/cancel
POST   /api/v1/subscriptions/addons/purchase → {"addon_slug": "...", "quantity": 1}
GET    /api/v1/subscriptions/admin/list     → Admin: all subscriptions
PATCH  /api/v1/subscriptions/admin/plans/{plan_id}

// Billing History
GET    /api/v1/billing/history
GET    /api/v1/billing/history/{record_id}
GET    /api/v1/billing/invoices
GET    /api/v1/billing/invoices/{invoice_id}
```

---

### 3.26 Financial Operating System (`/finance/*`)

**64 endpoints — the most powerful module**

```
// Dashboard & Overview
GET  /finance/dashboard?branch_id=          → 17-metric real-time dashboard
GET  /finance/trust-status                  → System health badges (for header)
GET  /finance/ca-view?period_start=&period_end= → CA/Accountant single-call view

// Reports
GET  /finance/reports/trial-balance?as_of=&branch_id=
GET  /finance/reports/balance-sheet?as_of=&branch_id=
GET  /finance/reports/income-statement?from_date=&to_date=&branch_id=
GET  /finance/reports/cash-flow?from_date=&to_date=

// Customer/Supplier Ledgers
GET  /finance/customers/aging
GET  /finance/customers/balances
GET  /finance/customers/{customer_id}/ledger
GET  /finance/suppliers/aging
GET  /finance/suppliers/balances
GET  /finance/suppliers/{supplier_id}/ledger

// Invoices
POST /finance/invoices                      → Create invoice
GET  /finance/invoices
GET  /finance/invoices/unpaid
GET  /finance/invoices/{invoice_id}
POST /finance/invoices/{invoice_id}/payment
POST /finance/invoices/{invoice_id}/void

// Expenses
POST /finance/expenses                      → Record expense
GET  /finance/expenses
GET  /finance/expenses/summary
GET  /finance/expenses/categories
POST /finance/expenses/categories
GET  /finance/expenses/{expense_id}
POST /finance/expenses/{expense_id}/approve

// Reconciliation
GET  /finance/reconciliation/summary
GET  /finance/reconciliation/statements
POST /finance/reconciliation/import-csv
POST /finance/reconciliation/auto-match
POST /finance/reconciliation/match
POST /finance/reconciliation/unmatch

// GST
GET  /finance/gst/summary
GET  /finance/gst/liabilities
POST /finance/gst/compute
POST /finance/gst/file
POST /finance/gst/pay

// GST Workflow (state machine)
POST /finance/gst/workflow/generate         → draft→generated
POST /finance/gst/workflow/{id}/review      → generated→reviewed
POST /finance/gst/workflow/{id}/export      → reviewed→exported
POST /finance/gst/workflow/{id}/file        → exported→filed
POST /finance/gst/workflow/{id}/pay         → filed→paid
GET  /finance/gst/workflow

// Daily Closing (state machine)
POST /finance/daily-close/init              → Compute expected cash
POST /finance/daily-close/cash-count        → Cashier enters actual amounts
POST /finance/daily-close/close             → Manager locks the day
GET  /finance/daily-close/history

// Insights (AI-powered)
GET  /finance/insights/profit?target_date=  → Why profit changed vs last week
GET  /finance/insights/channels             → Revenue by channel (dine-in/takeaway/delivery)
GET  /finance/insights/cash-mismatch        → Cash mismatch patterns

// Alerts
GET  /finance/alerts?severity=error|warning|info
POST /finance/alerts/scan                   → Run anomaly detection
POST /finance/alerts/{alert_id}/resolve     → Body: {"resolution_notes": "..."}
GET  /finance/alerts/summary                → Count by severity (for badges)

// Periods & Journals
GET  /finance/periods
POST /finance/periods/close
POST /finance/periods/reopen
GET  /finance/journals
POST /finance/journals/reverse

// Drilldown & Audit
GET  /finance/drilldown?account_id=&from=&to=
GET  /finance/integrity-check
GET  /finance/audit-log
GET  /finance/trend/revenue
POST /finance/views/refresh                 → Refresh materialized views
```

---

### 3.27 ERP Module (`/erp/*`)

```
// Chart of Accounts
GET  /erp/accounts | POST /erp/accounts | GET /erp/accounts/balances

// Journals
GET  /erp/journals | POST /erp/journals | POST /erp/journals/{id}/reverse

// Recipes
GET  /erp/recipes | POST /erp/recipes | PATCH /erp/recipes/{id}

// Inventory Ledger
GET  /erp/inventory-ledger/summary | GET /erp/inventory-ledger/history | POST /erp/inventory-ledger/adjust

// Vendors
GET  /erp/vendors | POST /erp/vendors | GET /erp/vendors/{id} | PATCH /erp/vendors/{id}

// GRN (Goods Received)
GET  /erp/grn | POST /erp/grn | PATCH /erp/grn/{id}/verify

// Vendor Payments
GET  /erp/vendor-payments | POST /erp/vendor-payments

// Cash Drawers & Shifts
GET  /erp/drawers | POST /erp/drawers
POST /erp/shifts/open | GET /erp/shifts/current | POST /erp/shifts/{id}/close | GET /erp/shifts

// Inter-Branch Transfers
GET  /erp/transfers | POST /erp/transfers
PATCH /erp/transfers/{id}/approve | PATCH /erp/transfers/{id}/ship | PATCH /erp/transfers/{id}/receive

// Tax Rates & Rules
GET  /erp/tax-rates | POST /erp/tax-rates | POST /erp/tax-rates/assign | DELETE /erp/tax-rates/assign/{item_id}/{tax_rate_id}
GET  /erp/tax-rules | POST /erp/tax-rules | PATCH /erp/tax-rules/{id} | DELETE /erp/tax-rules/{id}

// GST Reports
GET  /erp/gst-reports | POST /erp/gst-reports/generate | PATCH /erp/gst-reports/{id}/filed | GET /erp/gst-reports/tax-liability

// Profitability
GET  /erp/profitability | GET /erp/pnl/daily

// Platform Config
GET  /erp/platform-tax-config | POST /erp/platform-tax-config | PATCH /erp/platform-tax-config/{id}
GET  /erp/feature-flags | PATCH /erp/feature-flags/{flag_name}

// System
GET  /erp/consistency-check | GET /erp/event-log | GET /erp/order-summary/{order_id}

// Seed (one-time setup)
POST /erp/seed/feature-flags | POST /erp/seed/chart-of-accounts | POST /erp/seed/tax-rates
```

---

### 3.28 Other Modules

```
// Accounting
GET  /api/v1/accounting/entries | GET /accounting/cash-flow | GET /accounting/daily-breakdown
GET  /accounting/payment-methods | POST /accounting/expenses | POST /accounting/reversals
GET  /accounting/journals | POST /accounting/periods/close | POST /accounting/periods/reopen
GET  /accounting/periods | GET /accounting/rules | POST /accounting/rules
PUT  /accounting/rules/{rule_id} | DELETE /accounting/rules/{rule_id}
GET  /accounts | GET /accounts/tree | GET /accounts/{account_id}/ledger

// Cash Transactions
GET  /api/v1/cash-transactions | POST /cash-transactions | GET /cash-transactions/{id} | DELETE /cash-transactions/{id}

// Due Payments
GET  /api/v1/due-payments | POST /due-payments | GET /due-payments/{id}
POST /due-payments/{id}/pay | PATCH /due-payments/{id}/status

// Settlements
GET  /api/v1/settlements | POST /settlements | POST /settlements/{id}/reconcile
GET  /settlements/clearing-balance | GET /settlements/unsettled-payments

// Invoices (standalone)
POST /api/v1/invoices | GET /invoices | GET /invoices/unpaid | GET /invoices/{id}
POST /invoices/{id}/payment | POST /invoices/{id}/void

// Invoice Import (AI)
POST /api/v1/invoice-import/parse | POST /invoice-import/parse/upload
POST /invoice-import/confirm | GET /invoice-import/ | GET /invoice-import/{id}

// Expenses (standalone)
POST /api/v1/expenses | GET /expenses | GET /expenses/summary | GET /expenses/categories
POST /expenses/categories | GET /expenses/{id} | POST /expenses/{id}/approve

// Bank Reconciliation
POST /api/v1/bank-recon/import-csv | POST /bank-recon/statements | GET /bank-recon/statements
POST /bank-recon/auto-match | POST /bank-recon/match | POST /bank-recon/unmatch
POST /bank-recon/statements/{id}/exclude | GET /bank-recon/summary

// Sub-Ledger
GET  /api/v1/subledger/ar/balances | GET /subledger/ar/aging | GET /subledger/ar/{customer_id} | GET /subledger/ar/{customer_id}/balance
GET  /subledger/ap/balances | GET /subledger/ap/aging | GET /subledger/ap/{supplier_id} | GET /subledger/ap/{supplier_id}/balance

// Tax
POST /api/v1/tax/compute | GET /tax | GET /tax/gstr3b | GET /tax/{id} | POST /tax/{id}/file | POST /tax/{id}/pay

// Reports (standalone)
GET  /api/v1/reports/trial-balance | GET /reports/balance-sheet | GET /reports/income-statement
GET  /reports/customer-aging | GET /reports/supplier-aging | GET /reports/customer-statement/{id}
GET  /reports/supplier-statement/{id} | GET /reports/expense-summary | GET /reports/gst-summary
GET  /reports/cash-flow | GET /reports/drilldown | GET /reports/integrity-check
GET  /reports/pnl | GET /reports/cashflow

// Audit Logs
GET  /api/v1/audit-logs?entity_type=&entity_id=&action=&limit=50&offset=0

// Google Business
GET  /api/v1/google/connect | GET /google/callback | GET /google/status | POST /google/disconnect
GET  /google/locations | POST /google/locations/select | GET /google/reviews
POST /google/review/reply | POST /google/post | GET /google/post | GET /google/posts
GET  /google/insights | GET /google/insights/summary | POST /google/sync

// AI Features
POST /api/v1/ai-ingredients/suggest | POST /ai-ingredients/auto-link
POST /api/v1/menu-scan/ai-menu-scan | POST /menu-scan/base64 | POST /menu-scan/upload

// Food Images (AI)
POST /api/v1/food-images/generate | POST /food-images | GET /food-images/{name}

// Health
GET  /api/v1/health                         → {"status": "ok"}
GET  /api/v1/ready                          → Readiness check

// Voice
POST /api/v1/voice/tts | POST /voice/payment-notification

// Help
GET  /api/v1/help | GET /help/{article_id}

// Misc
GET  /api/v1/misc/sync-logs | GET /misc/payment-reminders | GET /misc/trial-status
GET  /misc/funnel-events | GET /misc/session-devices
```

---

## 4. WEBSOCKET (Real-time Updates)

### Staff WebSocket
```
ws://{BASE_URL}/ws?token=<access_token>
```

### Customer WebSocket (QR Dine-in)
```
ws://{BASE_URL}/ws/session?session_token=<session_token>
```

### Message Protocol:

**Server → Client:**
```json
{"event": "connected", "data": {"user_id": "...", "branch_id": "..."}}
{"event": "ping"}
{"event": "order.created", "data": {"order_id": "...", "items": [...]}}
{"event": "order.status_changed", "data": {"order_id": "...", "status": "preparing"}}
{"event": "kitchen.item_ready", "data": {"order_id": "...", "item_id": "..."}}
{"event": "table.session_updated", "data": {"session_id": "...", "table_id": "..."}}
{"event": "payment.received", "data": {"order_id": "...", "amount": 599}}
{"event": "delivery.status_changed", "data": {"delivery_id": "...", "status": "..."}}
{"event": "waitlist.updated", "data": {"entry_id": "..."}}
```

**Client → Server:**
```json
{"action": "pong"}
{"action": "subscribe", "channel": "entity:<order_id>"}
{"action": "unsubscribe", "channel": "entity:<order_id>"}
```

### Flutter WebSocket Setup:
```dart
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';

class BittuWebSocket {
  WebSocketChannel? _channel;
  Timer? _pongTimer;

  void connect(String baseUrl, String accessToken) {
    _channel = WebSocketChannel.connect(
      Uri.parse('ws://$baseUrl/ws?token=$accessToken'),
    );

    _channel!.stream.listen((message) {
      final data = jsonDecode(message);
      switch (data['event']) {
        case 'ping':
          _channel!.sink.add(jsonEncode({"action": "pong"}));
          break;
        case 'order.created':
          // Update orders list
          break;
        case 'order.status_changed':
          // Update order status in UI
          break;
        case 'kitchen.item_ready':
          // Play notification sound
          break;
      }
    });
  }

  void subscribeToOrder(String orderId) {
    _channel?.sink.add(jsonEncode({
      "action": "subscribe",
      "channel": "entity:$orderId"
    }));
  }

  void dispose() {
    _pongTimer?.cancel();
    _channel?.sink.close();
  }
}
```

---

## 5. ERROR HANDLING

Every error follows this format:
```json
{"detail": "Human-readable error message"}
```

| HTTP Code | Meaning | Flutter Action |
|-----------|---------|----------------|
| 401 | Token expired/invalid | Call token refresh, retry. If refresh fails → logout |
| 403 | Permission denied | Show "You don't have permission" toast |
| 404 | Not found | Show "Not found" |
| 409 | Conflict / business rule | Show detail message to user |
| 422 | Validation error | Highlight form fields |
| 429 | Rate limited | Retry after delay |

### Validation Error Shape (422):
```json
{
  "detail": [
    {
      "loc": ["body", "items", 0, "price"],
      "msg": "value is not a valid float",
      "type": "type_error.float"
    }
  ]
}
```

---

## 6. FLUTTER ARCHITECTURE

> ⚠️ **IMPORTANT**: The architecture described here was the v1 baseline.
> The full **enterprise-grade architecture** is now documented in
> [`FLUTTER_ENTERPRISE_ARCHITECTURE.md`](./FLUTTER_ENTERPRISE_ARCHITECTURE.md).
> Follow that document for all state management, caching, navigation, and
> network orchestration decisions. The rules there are non-negotiable for
> production-scale POS operations.

### Architecture Requirements (summary)

| Requirement | Rule |
|---|---|
| Providers | Created once at root (`main.dart`), **never** inside screens or routes |
| Tab navigation | `IndexedStack` — widgets stay alive, zero API calls on tab switch |
| Data loading | Cache-first: render L1/L2 cache instantly, refresh silently in background |
| API calls | All calls go through `RequestManager` (dedup, throttle, in-flight tracking) |
| Skeletons | Only shown when cache is empty AND loading for the first time |
| Finance module | Always-hot: `ensureLoaded()` is idempotent — data never wiped |
| Offline | Show stale cached data with timestamp; background retry when online |
| WS events | Patch specific items in provider — never trigger full reload |
| UI rebuilds | Use `Selector` for granular rebuilds — never `Consumer` on full provider |
| Memory | All timers and WS listeners explicitly cancelled in `dispose()` |

### Directory Structure

```
lib/
  main.dart                          ← Wire all persistent providers here
  app.dart                           ← MaterialApp + navigation scaffold
  core/
    network/
      api_client.dart                ← Dio with auth interceptor + auto-retry
      api_endpoints.dart             ← All endpoint constants
      request_manager.dart           ← Dedup / throttle / in-flight tracking
      retry_policy.dart              ← Exponential backoff
    cache/
      memory_cache.dart              ← L1 in-memory (Map + TTL)
      local_cache.dart               ← L2 Hive persistent store
      cache_policy.dart              ← TTL constants per resource type
    auth/
      auth_provider.dart             ← Token storage + refresh logic
      permission_guard.dart          ← Permission-based UI guard
    navigation/
      app_router.dart                ← GoRouter config
    websocket/
      ws_manager.dart                ← Singleton WS with auto-reconnect
  features/
    shell/
      main_shell.dart                ← IndexedStack persistent tab scaffold
    auth/                            ← Login, Google OAuth WebView
    dashboard/                       ← Owner/Manager dashboard
    orders/                          ← Order CRUD, status management
    kitchen/                         ← KDS (Kitchen Display System)
    tables/                          ← Table management, QR sessions
    menu/                            ← Items, Categories, Variants, Addons
    customers/                       ← CRM
    delivery/                        ← Delivery tracking
    staff/                           ← Branch users, invites, roles
    finance/                         ← Always-hot financial dashboard
    settings/                        ← Restaurant settings
    subscriptions/                   ← Plan management
    waitlist/                        ← Queue management
    analytics/                       ← Charts, heatmaps
  shared/
    widgets/
      skeleton_guard.dart            ← Shows skeleton only on first load
      stale_banner.dart              ← Offline stale data indicator
      finance_stat_card.dart         ← Flat finance card component
    utils/
      connectivity_monitor.dart      ← Online/offline state
      debouncer.dart
```

### API Client (enterprise pattern — see full version in `FLUTTER_ENTERPRISE_ARCHITECTURE.md`):

```dart
// Uses Dio with auto-retry (exponential backoff), token refresh interceptor,
// and request deduplication via RequestManager.
// See FLUTTER_ENTERPRISE_ARCHITECTURE.md § Rule 9 for complete implementation.
class ApiClient {
  late final Dio _dio;

  ApiClient(AuthProvider auth) {
    _dio = Dio(BaseOptions(
      baseUrl: 'https://api.bittupos.com/api/v1',
      connectTimeout: const Duration(seconds: 10),
      receiveTimeout: const Duration(seconds: 30),
    ));
    _dio.interceptors.addAll([
      _AuthInterceptor(auth, _dio),
      _RetryInterceptor(),   // exponential backoff, max 3 retries
    ]);
  }

  Future<Response<T>> get<T>(String path, {Map<String, dynamic>? params}) =>
      _dio.get(path, queryParameters: params);

  Future<Response<T>> post<T>(String path, {dynamic data}) =>
      _dio.post(path, data: data);

  Future<Response<T>> patch<T>(String path, {dynamic data}) =>
      _dio.patch(path, data: data);
}
```

---

## 7. FIRST-TIME USER FLOW

```
1. User opens app → Show login screen
2. User taps "Sign in with Google"
3. Call GET /auth/google → get URL → open in WebView
4. After consent → extract ?code= from redirect
5. Call POST /auth/google/callback?code=XXX → store tokens
6. Call GET /auth/me → get user profile
   - If restaurant_id exists → go to dashboard
   - If null → call POST /auth/initialize-restaurant → then dashboard
7. Call GET /auth/permissions/me → store permission map globally
8. Connect WebSocket for real-time updates
9. Show dashboard based on role
```

---

## 8. KEY IMPLEMENTATION NOTES

1. **All dates are ISO 8601**: `2026-04-20` or `2026-04-20T12:00:00Z`
2. **UUIDs everywhere**: All IDs are UUID strings
3. **Pagination**: Most list endpoints accept `?limit=50&offset=0` or `?page=1&page_size=50`
4. **Branch filtering**: Most endpoints accept `?branch_id=uuid` for multi-branch
5. **Idempotency**: Use `idempotency_key` in order creation to prevent duplicates
6. **File uploads**: Use `multipart/form-data` for image uploads (menu-scan, food-images, invoice-import)
7. **QR flows are public**: Dine-in QR endpoints do NOT need JWT — they use `session_token`
8. **WebSocket heartbeat**: Respond to `ping` with `pong` within 30 seconds or get disconnected

---

## WHAT TO BUILD FIRST (Priority Order)

1. **Auth + Login** (Google OAuth flow)
2. **Restaurant setup** (auto on first login)
3. **Menu management** (Categories → Items → Variants/Addons)
4. **Order management** (Create → Kitchen → Payment flow)
5. **Table management** (Tables + QR dine-in)
6. **Kitchen display** (real-time with WebSocket)
7. **Payments** (Cash + Online integration)
8. **Dashboard + Analytics**
9. **Staff management** (Branches, Roles, Invites)
10. **Finance** (Reports, GST, Daily closing)
11. **Delivery**
12. **Waitlist**
13. **Subscriptions**

---

## 9. COPY-PASTE PROMPT: OWNER + INVITED STAFF FLOW

Use the exact prompt below with your Flutter implementation assistant.

```text
Build the auth and staff onboarding flow exactly as below for Bittu POS.

Goal:
- First Google login user becomes owner.
- Owner can invite staff by email.
- Invited person logs in with Google and is mapped as branch staff automatically.

Base:
- Base URL: https://api.bittupos.com/api/v1
- Auth: Bearer access token for protected endpoints

Flow A: First-time owner login
1. Call GET /auth/google?redirect_to=<app_callback_url>
2. Open returned url in browser/webview
3. On callback, extract code
4. Call POST /auth/google/callback?code=<code>
5. Save access_token and refresh_token securely
6. Call GET /auth/me with bearer token
7. If restaurant_id is null, call POST /auth/initialize-restaurant
8. Call GET /auth/permissions/me and cache permissions for UI gating
9. Treat this user as owner when role = owner and is_branch_user = false

Flow B: Owner invites staff
1. Owner selects branch and role (manager/cashier/waiter/chef/staff)
2. Call POST /staff/invites with bearer token
3. Body:
{
  "branch_id": "<branch_uuid>",
  "email": "staff@email.com",
  "role": "cashier"
}
4. Show invite status from GET /staff/invites?branch_id=<branch_uuid>&status=pending

Flow C: Invited staff login
1. Invited user uses same Google login flow (auth/google -> callback exchange)
2. After token, call GET /auth/me
3. Backend auto-accepts pending invite for matching email
4. App must detect staff context:
- is_branch_user = true
- owner_id present
- role from invite
- branch_id assigned
5. Call GET /auth/permissions/me and render staff-limited UI

Fallback path (if no invite)
- If admin already knows staff user_id, owner can assign directly:
POST /staff/branch-users
{
  "branch_id": "<branch_uuid>",
  "user_id": "<supabase_user_uuid>",
  "role": "cashier"
}

Required app logic
- Global auth state stores:
  - access_token
  - refresh_token
  - role
  - is_branch_user
  - owner_id
  - branch_id
  - restaurant_id
  - permissions map
- On every app launch:
  1. validate/refresh token
  2. GET /auth/me
  3. GET /auth/permissions/me
  4. route user to owner dashboard or staff dashboard by role/is_branch_user

401 handling
- On 401, call POST /auth/token/refresh with refresh_token
- Retry original request once
- If refresh fails, force logout

Acceptance checks
- New Google user with no invite becomes owner.
- Owner can create invite and see pending status.
- Invited email logs in and lands as staff (not owner).
- Staff UI is permission-limited from /auth/permissions/me.
- Revoked invite cannot onboard as staff.
```
