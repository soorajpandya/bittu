"""API root router.

Mounts:
    /api/v1/...                 legacy (current production surface)
    /api/platform/v1/...        platform-admin domain (Phase-0 skeleton)
    /api/merchant/v1/...        merchant domain (Phase-0 skeleton)
    /api/branch/v1/...          branch/staff domain (Phase-0 skeleton)
    /api/public/v1/...          public/customer-facing (Phase-0 skeleton)
    /api/internal/v1/...        worker/M2M (Phase-0 skeleton, service-token gated)
    /api/financial/v1/...       financial infrastructure (Phase-0 skeleton)

The Phase-0 domain routers are mounted but currently expose no endpoints.
Handlers migrate into them per docs/ARCHITECTURE_V2.md §21.
"""
from fastapi import APIRouter

from app.api.v1 import (
    auth,
    orders,
    payments,
    kitchen,
    tables,
    inventory,
    staff,
    notifications,
    analytics,
    webhooks,
    health,
    items,
    # ── Payment gateways ──
    razorpay,
    phonepe,
    payu,
    paytm,
    cashfree,
    zivonpay,
    # ── Google Business Profile ──
    google_business,
    # ── KYC / Verification ──
    # (merchant-facing kyc/digilocker routers removed; admin-only via admin_merchant_kyc)
    # ── AI / Voice ──
    voice,
    menu_scan,
    # ── Menu / Catalog ──
    categories,
    combos,
    item_variants,
    item_addons,
    item_extras,
    item_ingredients,
    modifiers,
    item_stations,
    modifier_groups,
    menu,
    # ── Customers & CRM ──
    customers,
    customer_addresses,
    favourites,
    # ── Business Operations ──
    restaurant_settings,
    cash_transactions,
    due_payments,
    purchase_orders,
    # ── Billing / Admin ──
    billing,
    audit_logs,
    help_articles,
    misc,
    restaurants,
    restaurant_tables,
    table_events,
    table_sessions,
    kitchen_stations,
    ingredients,
    favourite_items,
    dinein,
    # ── ERP ──
    accounting,
    settlements,
    ai_ingredients,
    invoice_import,
    erp,
    # ── Finance: Statement & Settlement ──
    statements,
    # ── Food Images ──
    food_images,
    # ── Waitlist ──
    waitlist,
)

router = APIRouter(prefix="/api/v1")

router.include_router(auth.router)
router.include_router(orders.router)
router.include_router(payments.router)
router.include_router(kitchen.router)
router.include_router(tables.router)
router.include_router(inventory.router)
router.include_router(staff.router)
router.include_router(notifications.router)
router.include_router(analytics.router)
router.include_router(webhooks.router)
router.include_router(health.router)
router.include_router(items.router)
# ── Payment gateways ──
router.include_router(razorpay.router)
# ── Razorpay payment intents (Phase 2 deep integration) ──
from app.api.v1 import payment_intents as _payment_intents
router.include_router(_payment_intents.router)
# ── Razorpay settlements (Phase 6 deep integration) ──
from app.api.v1 import rzp_settlements as _rzp_settlements
router.include_router(_rzp_settlements.router)
# ── Razorpay Route — linked accounts + transfers (Phase 7 deep integration) ──
from app.api.v1 import rzp_route as _rzp_route
router.include_router(_rzp_route.router)
# ── Razorpay Route — raw passthrough endpoints (Postman parity) ──
from app.api.v1 import rzp_route_raw as _rzp_route_raw
router.include_router(_rzp_route_raw.router)
# ── Razorpay Smart Collect — virtual accounts + inbound credits (Phase 8) ──
from app.api.v1 import rzp_smart_collect as _rzp_smart_collect
router.include_router(_rzp_smart_collect.router)
# ── Razorpay Invoices — hosted invoices (Phase 9) ──
from app.api.v1 import rzp_invoices as _rzp_invoices
router.include_router(_rzp_invoices.router)
router.include_router(phonepe.router)
router.include_router(payu.router)
router.include_router(paytm.router)
router.include_router(cashfree.router)
router.include_router(zivonpay.router)
# ── Google Business Profile ──
router.include_router(google_business.router)
# ── AI / Voice ──
router.include_router(voice.router)
router.include_router(menu_scan.router)
# ── Menu / Catalog ──
router.include_router(categories.router)
router.include_router(combos.router)
router.include_router(item_variants.router)
router.include_router(item_addons.router)
router.include_router(item_extras.router)
router.include_router(item_ingredients.router)
router.include_router(modifiers.router)
router.include_router(item_stations.router)
router.include_router(modifier_groups.router)
router.include_router(menu.router)
# ── Customers & CRM ──
router.include_router(customers.router)
router.include_router(customer_addresses.router)
router.include_router(favourites.router)
# ── Business Operations ──
router.include_router(restaurant_settings.router)
router.include_router(cash_transactions.router)
router.include_router(due_payments.router)
router.include_router(purchase_orders.router)
# ── Billing / Admin ──
router.include_router(billing.router)
router.include_router(audit_logs.router)
router.include_router(help_articles.router)
router.include_router(misc.router)
router.include_router(restaurants.router)
router.include_router(restaurant_tables.router)
router.include_router(table_events.router)
router.include_router(table_sessions.router)
router.include_router(kitchen_stations.router)
router.include_router(ingredients.router)
router.include_router(favourite_items.router)
# ── Dine-In Sessions (QR v2) ──
router.include_router(dinein.router)
# ── ERP ──
router.include_router(accounting.router)
router.include_router(accounting.accounts_router)
router.include_router(accounting.reports_router)
router.include_router(settlements.router)
router.include_router(settlements.rules_router)
router.include_router(ai_ingredients.router)
router.include_router(invoice_import.router)
router.include_router(erp.router)
# ── Invoices / Expenses / Sub-Ledger / Tax ──
from app.api.v1 import invoices, expenses, subledger_tax, reports, bank_recon, finance
router.include_router(invoices.router)
router.include_router(expenses.router)
router.include_router(subledger_tax.subledger_router)
router.include_router(subledger_tax.tax_router)
# ── Financial Reports ──
router.include_router(reports.router)
# ── Bank Reconciliation ──
router.include_router(bank_recon.router)
# ── Cross-table Reconciliation Engine ──
from app.api.v1 import reconciliation as _reconciliation
router.include_router(_reconciliation.router)
# ── Merchant Wallet (cash + online + platform revenue) ──
from app.api.v1 import merchant_wallet as _merchant_wallet
router.include_router(_merchant_wallet.router)
# ── Merchant Ledger (immutable append-only money-movement ledger, Phase 1) ──
from app.api.v1 import merchant_ledger as _merchant_ledger
router.include_router(_merchant_ledger.router)
# ── Escrow Ledger (T+N held funds tracking, Phase 2) ──
from app.api.v1 import escrow as _escrow
router.include_router(_escrow.router)
# ── Bank Reconciliation Engine (Phase 3) — merchant + admin routers ──
from app.api.v1 import recon_engine as _recon_engine
router.include_router(_recon_engine.router)
from app.api.v1 import admin_recon_engine as _admin_recon_engine
router.include_router(_admin_recon_engine.router)
# ── Payouts / Disbursement Engine (Phase 4) — merchant + admin routers ──
from app.api.v1 import payouts as _payouts
router.include_router(_payouts.router)
from app.api.v1 import admin_payouts as _admin_payouts
router.include_router(_admin_payouts.router)
# ── Statements & Tax Invoices (Phase 5) — merchant + admin routers ──
from app.api.v1 import tax_invoices as _tax_invoices
router.include_router(_tax_invoices.router)
from app.api.v1 import merchant_statements as _merchant_statements
router.include_router(_merchant_statements.router)
from app.api.v1 import admin_tax_invoices as _admin_tax_invoices
router.include_router(_admin_tax_invoices.router)
from app.api.v1 import admin_merchant_statements as _admin_merchant_statements
router.include_router(_admin_merchant_statements.router)
# ── Audit & Compliance (Phase 6) — merchant + admin routers ──
from app.api.v1 import audit_events as _audit_events
router.include_router(_audit_events.router)
from app.api.v1 import admin_audit_events as _admin_audit_events
router.include_router(_admin_audit_events.router)
# ── Refunds & Disputes (Phase 7) — merchant + admin routers ──
from app.api.v1 import refunds as _refunds
router.include_router(_refunds.router)
from app.api.v1 import admin_refunds as _admin_refunds
router.include_router(_admin_refunds.router)
from app.api.v1 import disputes as _disputes
router.include_router(_disputes.router)
from app.api.v1 import admin_disputes as _admin_disputes
router.include_router(_admin_disputes.router)
# ── Financial Reports / Analytics (Phase 8) — merchant + admin routers ──
from app.api.v1 import fin_reports as _fin_reports
router.include_router(_fin_reports.router)
from app.api.v1 import admin_fin_reports as _admin_fin_reports
router.include_router(_admin_fin_reports.router)
# ── Per-Merchant Reports & Invoices (Bittu→Merchant SaaS PDF, customer tax invoice, txn ledger CSV) ──
from app.api.v1 import merchant_reports as _merchant_reports
router.include_router(_merchant_reports.router)
# ── Merchant KYC & Onboarding (Phase 9) — admin only ──
from app.api.v1 import admin_merchant_kyc as _admin_merchant_kyc
router.include_router(_admin_merchant_kyc.router)
# ── Fee Engine v2 (Phase 10) — admin only ──
from app.api.v1 import admin_fee_plans as _admin_fee_plans
router.include_router(_admin_fee_plans.router)
# ── Super Admin (top-level platform operations) ──
from app.api.v1 import super_admin as _super_admin
router.include_router(_super_admin.router)
# ── Cross-merchant pay-ins, settlements, webhook failures (Phase 11) ──
from app.api.v1 import admin_payments       as _admin_payments
from app.api.v1 import admin_settlements    as _admin_settlements
from app.api.v1 import admin_webhook_events as _admin_webhook_events
router.include_router(_admin_payments.router)
router.include_router(_admin_settlements.router)
router.include_router(_admin_webhook_events.router)
# ── Financial Operating System ──
router.include_router(finance.router)
# ── Statement & Settlement ──
router.include_router(statements.router)
# ── Food Images ──
router.include_router(food_images.router)
# ── Waitlist ──
router.include_router(waitlist.router)


# ──────────────────────────────────────────────────────────────────────
# Phase-0 (ARCHITECTURE_V2): mount new domain routers alongside legacy.
# These currently expose no endpoints — they are the cutover surface.
# Handlers migrate into them per docs/ARCHITECTURE_V2.md §21.
# ──────────────────────────────────────────────────────────────────────
from app.api.platform import router as _platform_router
from app.api.merchant import router as _merchant_router
from app.api.branch import router as _branch_router
from app.api.public import router as _public_router
from app.api.internal import router as _internal_router
from app.api.financial import router as _financial_router

_legacy_v1_router = router
router = APIRouter()
router.include_router(_legacy_v1_router)              # /api/v1/...   (already prefixed)

# ── Phase-2 (ARCHITECTURE_V2 §21): dual-mount admin routers under
# /api/platform/v1/admin/* so frontends can migrate per-endpoint.
# Legacy /api/v1/admin/* paths get a Deprecation/Sunset/Link header
# via DeprecationHeaderMiddleware. No URL is removed in this phase.
_platform_router.include_router(_admin_audit_events.router)
_platform_router.include_router(_admin_disputes.router)
_platform_router.include_router(_admin_refunds.router)
_platform_router.include_router(_admin_payouts.router)
_platform_router.include_router(_admin_recon_engine.router)
_platform_router.include_router(_admin_fee_plans.router)
_platform_router.include_router(_admin_fin_reports.router)
_platform_router.include_router(_admin_merchant_kyc.router)
_platform_router.include_router(_admin_merchant_statements.router)
_platform_router.include_router(_admin_tax_invoices.router)

router.include_router(_platform_router,  prefix="/api")  # /api/platform/v1/...
router.include_router(_merchant_router,  prefix="/api")  # /api/merchant/v1/...
router.include_router(_branch_router,    prefix="/api")  # /api/branch/v1/...
router.include_router(_public_router,    prefix="/api")  # /api/public/v1/...
router.include_router(_internal_router,  prefix="/api")  # /api/internal/v1/...
router.include_router(_financial_router, prefix="/api")  # /api/financial/v1/...
